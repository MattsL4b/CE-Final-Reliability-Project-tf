import os
import time
import json
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import logging
import boto3
import botocore
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# CONFIGURATION & ROUTE TTL RULES
# ---------------------------------------------------------------------------

# ADDED: Tiered TTL lookup based on endpoint path volatility
def get_ttl_config(path: str) -> tuple[int, int]:
    """
    Returns (soft_ttl_seconds, hard_ttl_seconds) based on endpoint stability.
    - soft_ttl: Window where cached data is considered completely fresh.
    - hard_ttl: Time-To-Live for DynamoDB automatic deletion.
    """
    if path.startswith("/hospitals"):
        return 900, 86400    # 15 mins fresh, 24 hours retention
    if path.startswith("/patients"):
        return 300, 43200    # 5 mins fresh, 12 hours retention
    if path.startswith("/notes"):
        return 300, 43200    # 5 mins fresh, 12 hours retention
    return 60, 7200          # 1 min fresh, 2 hours retention (default /notes)


HOSP_BASE_URL = os.environ["HOSP_BACKEND_URL"].rstrip("/")
CACHE_TABLE_NAME = os.environ["CACHE_TABLE_NAME"]

# MODIFIED: Removed fallback CACHE_TTL_SECONDS as we now use get_ttl_config()

RETRY_WRITES_ON_5XX = (
    os.environ.get("RETRY_WRITES_ON_5XX", "true").lower() == "true"
)

# One normal request + one retry.
MAX_ATTEMPTS = 1

# HOSP has already been observed taking several seconds to respond.
GET_TIMEOUT_SECONDS = 3.5
WRITE_TIMEOUT_SECONDS = 5.0

# Small delay before retrying.
RETRY_DELAY_SECONDS = 0.1

# Create the DynamoDB resource outside the handler for warm invocation reuse.
dynamodb = boto3.resource("dynamodb")
cache_table = dynamodb.Table(CACHE_TABLE_NAME)





# ---------------------------------------------------------------------------
# MAIN LAMBDA HANDLER
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """
    Main Lambda entry point.
    Handles route-aware caching, serve-stale-on-error, and mutation invalidation.
    """
    method = event.get("httpMethod", "GET").upper()
    path = event.get("path", "/")
    query_params = event.get("queryStringParameters") or {}
    incoming_headers = event.get("headers") or {}
    body = event.get("body")

    authorization = (
        incoming_headers.get("authorization") or incoming_headers.get("Authorization")
    )

    if not authorization:
        return build_response(
            status_code=401,
            body=json.dumps({"error": "Authentication required"}),
            cache_status="BYPASS"
        )
    
    if method == "GET":
        cache_key, resource_path = generate_cache_key(authorization, path, query_params)
        now = int(time.time())
        cached_item = get_from_cache(cache_key)

        # 1. Fresh Cache Hit 
        if cached_item and now < int(cached_item.get("soft_ttl", 0)):
            logger.info(f"CACHE HIT method={method} path={path} key={cache_key}")
            return build_response(
                status_code=200,
                body=cached_item["data"],
                cache_status="HIT"
            )

        # 2. Stale Cache Hit - Try upstream; fallback instantly to stale data if upstream fails
        if cached_item and "data" in cached_item:
            logger.info(f"CACHE STALE - Fetching revalidation key={cache_key}")
            soft_ttl_sec, hard_ttl_sec = get_ttl_config(path)

            upstream_body, upstream_status = fetch_from_hosp(
                method=method, path=path, query_params=query_params,
                incoming_headers=incoming_headers, authorization=authorization, body=body
            )
            
            if upstream_status == 200:
                save_to_cache(cache_key, resource_path, upstream_body, soft_ttl_sec, hard_ttl_sec)
                return build_response(
                    status_code=200,
                    body=upstream_body,
                    cache_status="HIT"
                )
            
            logger.warning(f"Revalidation failed (status={upstream_status}). Serving stale cache")
            return build_response(
                status_code=200,
                body=cached_item["data"],
                cache_status="STALE"
            )

        # 3. Cache Cold Miss - Fetch upstream and write to cache
        logger.info(f"CACHE COLD MISS method={method} path={path} key={cache_key}")
        soft_ttl_sec, hard_ttl_sec = get_ttl_config(path)

        upstream_body, upstream_status = fetch_from_hosp(
            method=method, path=path, query_params=query_params,
            incoming_headers=incoming_headers, authorization=authorization, body=body
        )

        if upstream_status == 200:
            save_to_cache(cache_key, resource_path, upstream_body, soft_ttl_sec, hard_ttl_sec)
            return build_response(
                status_code=200,
                body=upstream_body,
                cache_status="MISS"
            )

        return build_response(
            status_code=upstream_status,
            body=upstream_body or json.dumps({"error": "Upstream service failure"}),
            cache_status="MISS"
        )           

    # -----------------------------------------------------------------------
    # NON-GET MUTATIONS (POST, PUT, PATCH, DELETE)
    # -----------------------------------------------------------------------
    response_body, status_code = fetch_from_hosp(
        method=method,
        path=path,
        query_params=query_params,
        incoming_headers=incoming_headers,
        authorization=authorization,
        body=body
    )

    # Invalidate cache if write succeeded
    if status_code in {200, 201, 204}:
        invalidate_related_cache(path, authorization)

    return build_response(
        status_code=status_code,
        body=response_body,
        cache_status="BYPASS"
    )

# ---------------------------------------------------------------------------
# CACHE KEY GENERATION
# ---------------------------------------------------------------------------

# MODIFIED: Replaced build_cache_key with route-aware generate_cache_key
def generate_cache_key(authorization: str, path: str, query_params: dict) -> tuple[str, str]:
    """
    Generates deterministic, route-aware cache keys.
    Public/static routes (e.g. /hospitals) omit the authorization token
    so all users share a single cache entry.
    """
    normalized_path = path.rstrip("/") or "/"

    # strip transient and volatile dynamic parameters before hashing
    ignored_params = {"_", "timestamp", "cb", "cachebust", "request_id", "nonce"}
    filtered_params = {k: v for k, v in query_params.items() if k.lower() not in ignored_params}

    # Sort query string for deterministic keys
    query_string = urllib.parse.urlencode(sorted(filtered_params.items())) if filtered_params else ""
    resource = f"{normalized_path}?{query_string}" if query_string else normalized_path

    # Public route check: strip auth hash to allow shared cache across all users
    if normalized_path.startswith(("/hospitals", "/patients", "/notes")):
        raw_key = resource
    else:
        caller_hash = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        raw_key = f"{caller_hash}:{resource}"

    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return cache_key, normalized_path


# ---------------------------------------------------------------------------
# CACHE OPERATIONS (READ / WRITE / INVALIDATE)
# ---------------------------------------------------------------------------

def get_from_cache(key: str):
    """
    Retrieves cached item from DynamoDB if present.
    Returns the raw DynamoDB item dict or None.
    """
    try:
        response = cache_table.get_item(Key={"cache_key": key})
        return response.get("Item")
    except Exception as error:
        logger.error(f"CACHE READ ERROR: {str(error)}")
        return None


def save_to_cache(key: str, resource_path: str, body_data: str, soft_ttl_sec: int, hard_ttl_sec: int):
    """
    Saves payload with both soft_ttl (freshness) and hard_ttl (DynamoDB auto-cleanup).
    """
    now = int(time.time())
    try:
        cache_table.put_item(
            Item={
                "cache_key": key,
                "resource_path": resource_path,
                "data": body_data,
                "soft_ttl": now + soft_ttl_sec,
                "expires_at": now + hard_ttl_sec  # DynamoDB native TTL attribute name (ttl key in main.tf)
            }
        )
        logger.info(f"CACHE STORED key={key} soft_ttl={soft_ttl_sec}s hard_ttl={hard_ttl_sec}s")
    except Exception as error:
        logger.error(f"CACHE WRITE ERROR: {str(error)}")


def invalidate_related_cache(path: str, authorization: str = None):
    """
    Invalidates cached entries associated with base path on successful writes.
    """
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        return

    target_path = f"/{'/'.join(parts[:2])}" if len(parts) >= 2 else f"/{parts[0]}"
        
    try:
        response = cache_table.query(
            IndexName="ResourceIndex",
            KeyConditionExpression="resource_path = :path",
            ExpressionAttributeValues={":path": target_path},
            ProjectionExpression="cache_key"
        )
        for item in response.get("Items",[]):
            cache_table.delete_item(Key={"cache_key": item["cache_key"]})

        logger.info(f"CACHE INVALIDATED base_path={target_path}")
    except Exception as error:
        logger.error(f"CACHE INVALIDATION ERROR: {str(error)}")

# ---------------------------------------------------------------------------
# HOSP UPSTREAM REQUEST
# ---------------------------------------------------------------------------

def fetch_from_hosp(
    method,
    path,
    query_params,
    incoming_headers,
    authorization,
    body=None
):
    """
    Forwards HTTP request to legacy HOSP backend with retry handling.
    """
    url = f"{HOSP_BASE_URL}{path}"
    if query_params:
        url += f"?{urllib.parse.urlencode(query_params)}"

    outgoing_headers = {
        "Authorization": authorization,
        "Accept": "application/json"
    }

    content_type = (
        incoming_headers.get("content-type") or incoming_headers.get("Content-Type")
    )
    if content_type:
        outgoing_headers["Content-Type"] = content_type

    request_body = body.encode("utf-8") if body is not None else None
    timeout = GET_TIMEOUT_SECONDS if method == "GET" else WRITE_TIMEOUT_SECONDS

    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts_remaining = attempt < MAX_ATTEMPTS
        request = urllib.request.Request(
            url=url,
            data=request_body,
            headers=outgoing_headers,
            method=method
        )

        try:
            logger.info(f"HOSP REQUEST method={method} path={path} attempt={attempt}")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
                logger.info(f"HOSP RESPONSE method={method} path={path} status={response.status} attempt={attempt}")
                return response_body, response.status

        except urllib.error.HTTPError as error:
            status_code = error.code
            error_body = error.read().decode("utf-8")
            logger.warning(f"HOSP HTTP ERROR method={method} path={path} status={status_code} attempt={attempt}")

            transient_error = status_code in {500, 503}


            if method == "GET" and transient_error and attempts_remaining:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return error_body, status_code

        except urllib.error.URLError as error:
            status_code = 502
            logger.error(f"HOSP CONNECTION ERROR method={method} path={path} attempt={attempt} err={error}")
            if method == "GET" and attempts_remaining:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return json.dumps({"error": "Unable to contact HOSP"}), status_code

        except TimeoutError:
            status_code = 504
            logger.error(f"HOSP TIMEOUT method={method} path={path} attempt={attempt}")
            if method == "GET" and attempts_remaining:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return json.dumps({"error": "HOSP timed out"}), status_code

    return json.dumps({"error": "Unexpected proxy failure"}), 502


# ---------------------------------------------------------------------------
# ALB RESPONSE FORMATTER
# ---------------------------------------------------------------------------

def build_response(status_code, body, cache_status):
    if body is None:
        body = ""
    elif not isinstance(body, str):
        body = json.dumps(body)

    return {
        "isBase64Encoded": False,
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "X-Proxy-Cache": cache_status
        },
        "body": body
    }
