import os
import time
import json
import hashlib
import socket
import urllib.request
import urllib.error
import urllib.parse
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# CONFIGURATION & ROUTE TTL RULES
# ---------------------------------------------------------------------------

def get_ttl_config(path: str) -> tuple[int, int]:
    if path.startswith("/hospitals"):
        return 900, 86400    # 15 mins fresh, 24 hours retention
    if path.startswith("/patients"):
        return 300, 43200    # 5 mins fresh, 12 hours retention
    return 60, 7200          # 1 min fresh, 2 hours retention (default /notes)


HOSP_BASE_URL = os.environ["HOSP_BACKEND_URL"].rstrip("/")
CACHE_TABLE_NAME = os.environ["CACHE_TABLE_NAME"]

RETRY_WRITES_ON_5XX = os.environ.get("RETRY_WRITES_ON_5XX", "true").lower() == "true"
MAX_ATTEMPTS = 1  # Reduced to 1 to prevent doubling backend traffic during timeouts

UPSTREAM_TIMEOUT_SECONDS = 3.5

dynamodb = boto3.resource("dynamodb")
cache_table = dynamodb.Table(CACHE_TABLE_NAME)


# ---------------------------------------------------------------------------
# DISTRIBUTED LOCK (MUTEX) HELPERS
# ---------------------------------------------------------------------------

def acquire_lock(cache_key: str, lock_ttl_seconds: int = 20) -> bool:
    """Attempts to acquire a distributed lock in DynamoDB for a given cache key."""
    lock_key = f"LOCK#{cache_key}"
    now = int(time.time())
    expires_at = now + lock_ttl_seconds

    try:
        cache_table.put_item(
            Item={
                "cache_key": lock_key,
                "ttl": expires_at
            },
            ConditionExpression="attribute_not_exists(cache_key) OR #t < :now",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":now": now}
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        logger.error(f"LOCK ACQUIRE ERROR: {str(e)}")
        return False

def release_lock(cache_key: str):
    """Releases the distributed lock in DynamoDB."""
    lock_key = f"LOCK#{cache_key}"
    try:
        cache_table.delete_item(Key={"cache_key": lock_key})
    except Exception as e:
        logger.error(f"LOCK RELEASE ERROR: {str(e)}")


# ---------------------------------------------------------------------------
# CACHE OPERATIONS & KEY GENERATION
# ---------------------------------------------------------------------------

def generate_cache_key(authorization: str, path: str, query_params: dict) -> tuple[str, str]:
    normalized_path = path.rstrip("/") or "/"
    query_string = urllib.parse.urlencode(sorted(query_params.items())) if query_params else ""
    resource = f"{normalized_path}?{query_string}" if query_string else normalized_path

    if normalized_path.startswith("/hospitals"):
        raw_key = resource
    else:
        caller_hash = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        raw_key = f"{caller_hash}:{resource}"

    cache_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return cache_key, normalized_path


def get_from_cache(key: str):
    try:
        response = cache_table.get_item(Key={"cache_key": key})
        return response.get("Item")
    except Exception as error:
        logger.error(f"CACHE READ ERROR: {str(error)}")
        return None


def save_to_cache(key: str, resource_path: str, body_data: str, soft_ttl_sec: int, hard_ttl_sec: int):
    now = int(time.time())
    try:
        cache_table.put_item(
            Item={
                "cache_key": key,
                "resource_path": resource_path,
                "data": body_data,
                "soft_ttl": now + soft_ttl_sec,
                "ttl": now + hard_ttl_sec
            }
        )
        logger.info(f"CACHE STORED key={key} soft_ttl={soft_ttl_sec}s hard_ttl={hard_ttl_sec}s")
    except Exception as error:
        logger.error(f"CACHE WRITE ERROR: {str(error)}")


def invalidate_related_cache(path: str, authorization: str = None):
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
        for item in response.get("Items", []):
            cache_table.delete_item(Key={"cache_key": item["cache_key"]})

        logger.info(f"TARGETED CACHE INVALIDATED path={target_path}")
    except Exception as error:
        logger.error(f"CACHE INVALIDATION ERROR: {str(error)}")


# ---------------------------------------------------------------------------
# UPSTREAM REQUEST & RESPONSE HELPERS
# ---------------------------------------------------------------------------

def fetch_from_hosp(method, path, query_params, incoming_headers, authorization, body=None):
    url = f"{HOSP_BASE_URL}{path}"
    if query_params:
        url += f"?{urllib.parse.urlencode(query_params)}"

    outgoing_headers = {
        "Authorization": authorization,
        "Accept": "application/json"
    }

    # Case-insensitive header check for Content-Type
    headers_lower = {k.lower(): v for k, v in incoming_headers.items()}
    if "content-type" in headers_lower:
        outgoing_headers["Content-Type"] = headers_lower["content-type"]

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
            return error_body, status_code

        except urllib.error.URLError as error:
            if isinstance(error.reason, socket.timeout):
                logger.error(f"HOSP TIMEOUT method={method} path={path} attempt={attempt}")
                return json.dumps({"error": "HOSP timed out"}), 504
            
            logger.error(f"HOSP CONNECTION ERROR method={method} path={path} attempt={attempt}")
            return json.dumps({"error": "Unable to contact HOSP"}), 502

        except socket.timeout:
            logger.error(f"HOSP TIMEOUT method={method} path={path} attempt={attempt}")
            return json.dumps({"error": "HOSP timed out"}), 504

    return json.dumps({"error": "Unexpected proxy failure"}), 502


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


# ---------------------------------------------------------------------------
# MAIN LAMBDA HANDLER
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    method = event.get("httpMethod", "GET").upper()
    path = event.get("path", "/")
    query_params = event.get("queryStringParameters") or {}
    incoming_headers = event.get("headers") or {}
    body = event.get("body")

    headers_lower = {k.lower(): v for k, v in incoming_headers.items()}
    authorization = headers_lower.get("authorization")

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

        # Fresh cache hit check (Soft TTL)
        if cached_item and now < int(cached_item.get("soft_ttl", 0)):
            logger.info(f"CACHE HIT method={method} path={path} key={cache_key}")
            return build_response(
                status_code=200,
                body=cached_item["data"],
                cache_status="HIT"
            )

        # Stale-While-Revalidate (SWR) path or Cold Miss
        got_lock = acquire_lock(cache_key, lock_ttl_seconds=20)

        if not got_lock:
            if cached_item and "data" in cached_item:
                logger.info(f"LOCKED - Serving stale cache while another request updates key={cache_key}")
                return build_response(
                    status_code=200,
                    body=cached_item["data"],
                    cache_status="STALE_LOCKED"
                )
            
            # Cold Miss + Locked: Poll briefly for in-flight fetch
            logger.info(f"LOCKED COLD MISS - Waiting for in-flight fetch key={cache_key}")
            for _ in range(5):
                time.sleep(0.5)
                cached_item = get_from_cache(cache_key)
                if cached_item and "data" in cached_item:
                    return build_response(
                        status_code=200,
                        body=cached_item["data"],
                        cache_status="HIT_AFTER_WAIT"
                    )
            
            return build_response(
                status_code=503,
                body=json.dumps({"error": "Backend busy, please retry shortly"}),
                cache_status="MISS_LOCKED"
            )

        # Successfully acquired lock: Perform fetch
        try:
            logger.info(f"LOCK ACQUIRED - Fetching from upstream path={path} key={cache_key}")
            soft_ttl_sec, hard_ttl_sec = get_ttl_config(path)
            
            response_body, status_code = fetch_from_hosp(
                method=method,
                path=path,
                query_params=query_params,
                incoming_headers=incoming_headers,
                authorization=authorization,
                body=body
            )

            # Upstream failure handling with stale fallback
            if status_code in {502, 504} and cached_item and "data" in cached_item:
                logger.warning(f"UPSTREAM FAILED - Falling back to stale cached data key={cache_key}")
                return build_response(
                    status_code=200,
                    body=cached_item["data"],
                    cache_status="STALE_FALLBACK"
                )

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

            return build_response(
                status_code=status_code,
                body=response_body,
                cache_status="MISS"
            )
        finally:
            release_lock(cache_key)

    # NON-GET MUTATIONS
    response_body, status_code = fetch_from_hosp(
        method=method,
        path=path,
        query_params=query_params,
        incoming_headers=incoming_headers,
        authorization=authorization,
        body=body
    )

    if status_code in {200, 201, 204}:
        invalidate_related_cache(path, authorization)

    return build_response(
        status_code=status_code,
        body=response_body,
        cache_status="BYPASS"
    )