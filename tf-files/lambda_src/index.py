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
    return 60, 7200          # 1 min fresh, 2 hours retention (default /notes)


HOSP_BASE_URL = os.environ["HOSP_BACKEND_URL"].rstrip("/")
CACHE_TABLE_NAME = os.environ["CACHE_TABLE_NAME"]

# MODIFIED: Removed fallback CACHE_TTL_SECONDS as we now use get_ttl_config()

RETRY_WRITES_ON_5XX = (
    os.environ.get("RETRY_WRITES_ON_5XX", "false").lower() == "true"
)

# One normal request + one retry.
MAX_ATTEMPTS = 2

# HOSP has already been observed taking several seconds to respond.
UPSTREAM_TIMEOUT_SECONDS = 12

# Small delay before retrying.
RETRY_DELAY_SECONDS = 0.2

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
        # -----------------------------------------------------------------------
    # GET REQUEST PROCESSING: FRESH HIT / UPSTREAM FETCH / SERVE STALE
    # -----------------------------------------------------------------------
    if method == "GET":
        # MODIFIED: Generates route-aware key (shared for /hospitals, token-bound for others)
        cache_key, resource_path = generate_cache_key(authorization, path, query_params)
        now = int(time.time())
        
        cached_item = get_from_cache(cache_key)

        # 1. Fresh Cache Hit (now < soft_ttl)
        if cached_item and now < int(cached_item.get("soft_ttl", 0)):
            logger.info(f"CACHE HIT method={method} path={path} key={cache_key}")
            return build_response(
                status_code=200,
                body=cached_item["data"],
                cache_status="HIT"
            )

        logger.info(f"CACHE MISS/EXPIRED method={method} path={path} key={cache_key}")

        # 2. Upstream Fetch
        soft_ttl_sec, hard_ttl_sec = get_ttl_config(path)
        
        response_body, status_code = fetch_from_hosp(
            method=method,
            path=path,
            query_params=query_params,
            incoming_headers=incoming_headers,
            authorization=authorization,
            body=body
        )

        # 3. Successful Upstream Response: Store to Cache
        if status_code == 200:
            save_to_cache(
                key=cache_key,
                resource_path=resource_path,
                body_data=response_body,
                soft_ttl_sec=soft_ttl_sec,
                hard_ttl_sec=hard_ttl_sec
            )
            return build_response(
                status_code=200,
                body=response_body,
                cache_status="MISS"
            )

        # 4. ADDED: Serve Stale on Upstream Error Fallback
        if cached_item and "data" in cached_item:
            logger.warning(f"UPSTREAM FAILED (status={status_code}). SERVING STALE DATA key={cache_key}")
            return build_response(
                status_code=200,
                body=cached_item["data"],
                cache_status="STALE"
            )

        # 5. Upstream Failed & No Cache Backup Available
        return build_response(
            status_code=status_code,
            body=response_body,
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
        invalidate_related_cache(path)

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
    
    # Sort query string for deterministic keys
    query_string = urllib.parse.urlencode(sorted(query_params.items())) if query_params else ""
    resource = f"{normalized_path}?{query_string}" if query_string else normalized_path

    # Public route check: strip auth hash to allow shared cache across all users
    if normalized_path in ["/hospitals"]:
        raw_key = resource
    else:
        caller_hash = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        raw_key = f"{caller_hash}:{resource}"

    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return cache_key, normalized_path


# ---------------------------------------------------------------------------
# CACHE OPERATIONS (READ / WRITE / INVALIDATE)
# ---------------------------------------------------------------------------

# MODIFIED: Updated to retrieve full item for Soft TTL + Hard TTL inspection
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


# MODIFIED: Updated to accept soft_ttl and hard_ttl parameters and store JSON payloads
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
                "ttl": now + hard_ttl_sec  # DynamoDB native TTL attribute name
            }
        )
        logger.info(f"CACHE STORED key={key} soft_ttl={soft_ttl_sec}s hard_ttl={hard_ttl_sec}s")
    except Exception as error:
        logger.error(f"CACHE WRITE ERROR: {str(error)}")


def invalidate_related_cache(path: str):
    """
    Invalidates cached entries associated with base path on successful writes.
    """
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        return

    base_path = f"/{parts[0]}"

    try:
        response = cache_table.query(
            IndexName="ResourceIndex",
            KeyConditionExpression="resource_path = :path",
            ExpressionAttributeValues={":path": base_path},
            ProjectionExpression="cache_key"
        )
        items_to_delete = response.get("Items", [])
        for item in items_to_delete:
            cache_table.delete_item(Key={"cache_key": item["cache_key"]})

        logger.info(f"CACHE INVALIDATED base_path={base_path}")
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

    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url=url,
            data=request_body,
            headers=outgoing_headers,
            method=method
        )

        try:
            logger.info(f"HOSP REQUEST method={method} path={path} attempt={attempt}")
            with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
                response_body = response.read().decode("utf-8")
                logger.info(f"HOSP RESPONSE method={method} path={path} status={response.status} attempt={attempt}")
                return response_body, response.status

        except urllib.error.HTTPError as error:
            status_code = error.code
            error_body = error.read().decode("utf-8")
            logger.warning(f"HOSP HTTP ERROR method={method} path={path} status={status_code} attempt={attempt}")

            transient_error = status_code in {500, 503}
            attempts_remaining = attempt < MAX_ATTEMPTS

            if method == "GET" and transient_error and attempts_remaining:
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            if (
                method in {"POST", "PUT", "PATCH", "DELETE"}
                and RETRY_WRITES_ON_5XX
                and transient_error
                and attempts_remaining
            ):
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            return error_body, status_code

        except urllib.error.URLError:
            logger.error(f"HOSP CONNECTION ERROR method={method} path={path} attempt={attempt}")
            if method == "GET" and attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return json.dumps({"error": "Unable to contact HOSP"}), 502

        except TimeoutError:
            logger.error(f"HOSP TIMEOUT method={method} path={path} attempt={attempt}")
            if method == "GET" and attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return json.dumps({"error": "HOSP timed out"}), 504

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
