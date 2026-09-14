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
# CONFIGURATION
# ---------------------------------------------------------------------------

def get_ttl_config(path:str):
    if path.startswith("/hospitals"):
        return 900, 86400 # 15 mins fresh, 24 hours retention
    if path.startswith("/patients"):
        return 300, 43200
    return 60, 7200

def generate_cache_key(auth_token: str, path: str, query_string: str):
    if path in ["/hospitals"]:
        raw_key = f"{path}?{query_string}"
    else:
        raw_key = f"{auth_token}:{path}?{query_string}"
    return hashlib.sha256(raw_key.encode('utf-8')).hexdige

    
HOSP_BASE_URL = os.environ["HOSP_BACKEND_URL"].rstrip("/")
#changed name below from chache_table_name
CACHE_TABLE_NAME = os.environ["CACHE_TABLE_NAME"]


CACHE_TTL_SECONDS = int(
    os.environ.get("CACHE_TTL_SECONDS", "60")
)

RETRY_WRITES_ON_5XX = (
    os.environ.get("RETRY_WRITES_ON_5XX", "false").lower() == "true"
)

# One normal request + one retry.
MAX_ATTEMPTS = 2

# HOSP has already been observed taking several seconds to respond.
UPSTREAM_TIMEOUT_SECONDS = 12

# Small delay before retrying.
RETRY_DELAY_SECONDS = 0.2


# Create the DynamoDB resource outside the handler.
# Lambda can reuse this object across warm invocations.
dynamodb = boto3.resource("dynamodb")
cache_table = dynamodb.Table(CACHE_TABLE_NAME)
table = dynamodb.Table(CACHE_TABLE_NAME)


# ---------------------------------------------------------------------------
# LAMBDA HANDLER
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    """
    Entry point called by AWS Lambda.

    Takes the ALB request event, checks the cache for GET requests,
    forwards requests to HOSP where necessary, and returns an
    ALB-compatible response.
    """

    method = event.get("httpMethod", "GET").upper()
    path = event.get("path", "/")
    query_params = event.get("queryStringParameters") or {}
    incoming_headers = event.get("headers") or {}
    body = event.get("body")

    # -----------------------------------------------------------------------
    # AUTHENTICATION
    # -----------------------------------------------------------------------

    # Header names are case-insensitive, but AWS may represent them using
    # different casing.
    authorization = (
        incoming_headers.get("authorization")
        or incoming_headers.get("Authorization")
    )

    if not authorization:
        return build_response(
            status_code=401,
            body=json.dumps({
                "error": "Authentication required"
            }),
            cache_status="BYPASS"
        )

    # -----------------------------------------------------------------------
    # GET REQUESTS: CHECK CACHE FIRST
    # -----------------------------------------------------------------------

    cache_key = None
    resource_path = None

    if method == "GET":

        cache_key, resource_path = build_cache_key(
            authorization,
            path,
            query_params
        )

        cached_body = get_from_cache(cache_key)

        if cached_body is not None:

            print(
                f"CACHE HIT method={method} path={path} key={cache_key} " 
            )

            return build_response(
                status_code=200,
                body=cached_body,
                cache_status="HIT"
            )

        print(
            f"CACHE MISS method={method} path={path} key={cache_key}"
        )

    # -----------------------------------------------------------------------
    # CALL HOSP
    # -----------------------------------------------------------------------

    response_body, status_code = fetch_from_hosp(
        method=method,
        path=path,
        query_params=query_params,
        incoming_headers=incoming_headers,
        authorization=authorization,
        body=body
    )

    # -----------------------------------------------------------------------
    # CACHE SUCCESSFUL GET RESPONSES
    # -----------------------------------------------------------------------

    if method == "GET" and status_code == 200:

        save_to_cache(
            key=cache_key,
            resource_path=resource_path,
            body=response_body,
            ttl_seconds=CACHE_TTL_SECONDS
        )

    # -----------------------------------------------------------------------
    # INVALIDATE CACHE AFTER SUCCESSFUL WRITES
    # -----------------------------------------------------------------------

    if (
        method in {"POST", "PUT", "PATCH", "DELETE"}
        and status_code in {200, 201, 204}
    ):
        invalidate_related_cache(path)

    return build_response(
        status_code=status_code,
        body=response_body,
        cache_status="MISS" if method == "GET" else "BYPASS"
    )


# ---------------------------------------------------------------------------
# CACHE KEY
# ---------------------------------------------------------------------------

def build_cache_key(authorization, path, query_params):
    """
    Creates a cache key unique to both:
        - the authenticated caller
        - the requested resource

    We hash the Authorization header so credentials themselves are never
    written to DynamoDB.

    Example conceptual key:

        7e63ab...:/notes?patient_id=3
    """
    normalized_path = path.rstrip("/") or "/"


    caller_hash = hashlib.sha256(
        authorization.encode("utf-8")
    ).hexdigest()

    query_string = urllib.parse.urlencode(
        sorted(query_params.items())
    )

    resource = normalized_path

    if query_string:
        resource = f"{normalized_path}?{query_string}"
    cache_key = f"{caller_hash}:{resource}"

    return cache_key, normalized_path


# ---------------------------------------------------------------------------
# CACHE READ
# ---------------------------------------------------------------------------

def get_from_cache(key):
    """
    Attempts to retrieve a cached response from DynamoDB.

    DynamoDB TTL deletion is asynchronous, so an expired item might remain
    in the table for some time. We therefore check ttl_timestamp ourselves
    before using the value.
    """

    try:

        response = cache_table.get_item(
            Key={
                "cache_key": key
            }
        )

        item = response.get("Item")

        if not item:
            return None

        expires_at = int(item["expires_at"])
        current_time = int(time.time())

        if expires_at <= current_time:
            return None

        # Keep this as a string because ALB expects body to be a string.
        return item["data"]

    except Exception as error:

        # Cache failure should not make the HOSP service unavailable.
        print(
            f"CACHE READ ERROR: {str(error)} "
            
        )

        return None


# ---------------------------------------------------------------------------
# CACHE WRITE
# ---------------------------------------------------------------------------

def save_to_cache(key, resource_path, body, ttl_seconds):
    """
    Saves a successful GET response in DynamoDB.
    """

    try:

        expires_at = int(time.time()) + ttl_seconds

        cache_table.put_item(
            Item={
                "cache_key": key,
                "resource_path": resource_path,
                "data": body,
                "expires_at": expires_at
            }
        )
        print(f"CACHE STORED key={key} ttl={ttl_seconds}s")

    except Exception as error:

        # Caching is an optimisation. A failed cache write should not
        # turn a successful HOSP response into a failed user request.
        print(
            f"CACHE WRITE ERROR: {str(error)}"
        )


# ---------------------------------------------------------------------------
# CACHE INVALIDATION
# ---------------------------------------------------------------------------

def invalidate_related_cache(path):
    """
    Removes cached entries which may now be stale after a successful write.

    Because cache keys contain a caller hash, and because multiple query
    variants can exist, invalidation requires looking for cache keys
    containing the relevant resource prefix.

    This uses a DynamoDB scan.

    That would not be desirable for a large production cache, but is
    acceptable as an initial bootcamp implementation with a small table.

    A production design would likely use a better cache-versioning or
    invalidation strategy.
    """

    # Work out the collection/resource that may have changed.
    #
    # Examples:
    #   /notes/12     -> /notes
    #   /patients/3   -> /patients
    #   /hospitals/4  -> /hospitals

    parts = path.strip("/").split("/")

    if not parts or not parts[0]:
        return

    base_path = f"/{parts[0]}"

    try:
        response = cache_table.query(
            IndexName="ResourceIndex",
            KeyConditionExpression="resource_path = :path",
            ExpressionAttributeValues={
                ":path": base_path
            },
        
            ProjectionExpression="cache_key"
        )
        items_to_delete = response.get("Items", [])
        for item in items_to_delete:
            cache_table.delete_item(
                Key={
                    "cache_key": item["cache_key"]
                }
            )

        print(
            f"CACHE INVALIDATED base_path={base_path}"
        )

    except Exception as error:

        # Again: failure of the cache should not fail the user's write.
        print(
            f"CACHE INVALIDATION ERROR: {str(error)}"
        )


# ---------------------------------------------------------------------------
# HOSP REQUEST
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
    Forwards a request to the legacy HOSP backend.

    Retry policy:

        GET:
            Retry once on:
                - HTTP 500
                - HTTP 503
                - connection failure
                - timeout

        POST / PUT / PATCH / DELETE:
            By default, do NOT retry.

            If RETRY_WRITES_ON_5XX=true, retry a 500 or 503 once.

            We deliberately do not automatically retry writes after
            ambiguous network failures/timeouts because HOSP may already
            have committed the change.
    """

    url = f"{HOSP_BASE_URL}{path}"

    if query_params:

        query_string = urllib.parse.urlencode(
            query_params
        )

        url += f"?{query_string}"

    # -----------------------------------------------------------------------
    # HEADERS
    # -----------------------------------------------------------------------

    outgoing_headers = {
        "Authorization": authorization,
        "Accept": "application/json"
    }

    # Preserve Content-Type for POST/PATCH/etc.
    content_type = (
        incoming_headers.get("content-type")
        or incoming_headers.get("Content-Type")
    )

    if content_type:
        outgoing_headers["Content-Type"] = content_type

    # -----------------------------------------------------------------------
    # BODY
    # -----------------------------------------------------------------------

    request_body = None

    if body is not None:

        # ALB normally gives Lambda the request body as a string.
        request_body = body.encode("utf-8")

    # -----------------------------------------------------------------------
    # ATTEMPTS
    # -----------------------------------------------------------------------

    for attempt in range(1, MAX_ATTEMPTS + 1):

        request = urllib.request.Request(
            url=url,
            data=request_body,
            headers=outgoing_headers,
            method=method
        )

        try:

            print(
                f"HOSP REQUEST "
                f"method={method} "
                f"path={path} "
                f"attempt={attempt}"
            )

            with urllib.request.urlopen(
                request,
                timeout=UPSTREAM_TIMEOUT_SECONDS
            ) as response:

                response_body = (
                    response.read().decode("utf-8")
                )

                print(
                    f"HOSP RESPONSE "
                    f"method={method} "
                    f"path={path} "
                    f"status={response.status} "
                    f"attempt={attempt}"
                )

                return (
                    response_body,
                    response.status
                )

        # -------------------------------------------------------------------
        # HOSP RETURNED AN HTTP ERROR
        # -------------------------------------------------------------------

        except urllib.error.HTTPError as error:

            status_code = error.code
            error_body = (
                error.read().decode("utf-8")
            )

            print(
                f"HOSP HTTP ERROR "
                f"method={method} "
                f"path={path} "
                f"status={status_code} "
                f"attempt={attempt}"
            )

            transient_error = (
                status_code in {500, 503}
            )

            attempts_remaining = (
                attempt < MAX_ATTEMPTS
            )

            # GET requests are safe to repeat.
            if (
                method == "GET"
                and transient_error
                and attempts_remaining
            ):

                print(
                    f"RETRYING GET path={path}"
                )

                time.sleep(
                    RETRY_DELAY_SECONDS
                )

                continue

            # Optional write retry.
            if (
                method in {"POST", "PUT", "PATCH", "DELETE"}
                and RETRY_WRITES_ON_5XX
                and transient_error
                and attempts_remaining
            ):

                print(
                    f"RETRYING WRITE "
                    f"method={method} "
                    f"path={path}"
                )

                time.sleep(
                    RETRY_DELAY_SECONDS
                )

                continue

            # Return HOSP's actual HTTP error.
            return (
                error_body,
                status_code
            )

        # -------------------------------------------------------------------
        # NETWORK / DNS / CONNECTION FAILURE
        # -------------------------------------------------------------------

        except urllib.error.URLError:

            print(
                f"HOSP CONNECTION ERROR "
                f"method={method} "
                f"path={path} "
                f"attempt={attempt}"
            )

            # A GET can safely be repeated.
            if (
                method == "GET"
                and attempt < MAX_ATTEMPTS
            ):

                time.sleep(
                    RETRY_DELAY_SECONDS
                )

                continue

            # For a write, we don't know whether HOSP performed the action.
            return (
                json.dumps({
                    "error": "Unable to contact HOSP"
                }),
                502
            )

        # -------------------------------------------------------------------
        # TIMEOUT
        # -------------------------------------------------------------------

        except TimeoutError:

            print(
                f"HOSP TIMEOUT "
                f"method={method} "
                f"path={path} "
                f"attempt={attempt}"
            )

            if (
                method == "GET"
                and attempt < MAX_ATTEMPTS
            ):

                time.sleep(
                    RETRY_DELAY_SECONDS
                )

                continue

            return (
                json.dumps({
                    "error": "HOSP timed out"
                }),
                504
            )

    # Should normally never reach here.
    return (
        json.dumps({
            "error": "Unexpected proxy failure"
        }),
        502
    )


# ---------------------------------------------------------------------------
# ALB RESPONSE
# ---------------------------------------------------------------------------

def build_response(
    status_code,
    body,
    cache_status
):
    """
    Formats a response in the structure expected by an ALB Lambda target.

    X-Proxy-Cache makes testing easier in Insomnia:

        HIT
            Response came from DynamoDB.

        MISS
            Cache did not contain the response, so HOSP was contacted.

        BYPASS
            Request was not eligible for caching.
    """

    # ALB requires the body to be a string.
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

# ------------------------------------------------------------------
# HANDLER HELPERS
# ------------------------------------------------------------------

def get_cached_item(cache_key: str):
    try:
        response = table.get_item(Key={'cache_key': cache_key})
        return response.get('Item')
    except (BotoCoreError, ClientError) as e:
        logger.error(f"DynamoDB read error: {str(e)}")
    return None

def save_to_cache(cache_key: str, payload: dict, soft_ttl_sec: int, hard_ttl_sec: int):
    now = int(time.time())
    try:
        table.put_item(
            Item={
                'cache_key': cache_key,
                'payload': json.dumps(payload),
                'soft_ttl': now + soft_ttl_sec,
                'ttl': now + hard_ttl_sec # Used by DynamoDB TTL feature
            }
        )
    except (BotoCoreError, ClientError) as e:
        logger.error(f"DynamoDB write error: {str(e)}")


# ------------------------------------------------------------------
# REQUEST PROCESSING LOGIC
# ------------------------------------------------------------------

def handle_get_request(auth_token: str, path: str, query_string: str):
    cache_key = generate_cache_key(auth_token, path, query_string)
    now = int(time.time())

    cached_item = get_cached_item(cache_key)

    # 1. Fresh Cache Hit
    if cached_item and now < cached_item.get('soft_ttl', 0):
        logger.info(f"CACHE HIT path={path} key={cache_key}")
        return build_response(200, json.loads(cached_item['payload']))

    logger.info(f"CACHE MISS/EXPIRED path={path} key={cache_key}")

    # 2. Attempt Upstream Fetch
    soft_ttl_sec, hard_ttl_sec = get_ttl_config(path)
    try:
        upstream_res = fetch_from_hosp(path, query_string, auth_token)

        if upstream_res.status_code == 200:
            payload = upstream_res.json()
            save_to_cache(cache_key, payload, soft_ttl_sec, hard_ttl_sec)
            return build_response(200, payload)
        else:
            raise Exception(f"Upstream returned status {upstream_res.status_code}")

    except Exception as err:
        logger.warning(f"Upstream request failed: {str(err)}")

        # 3. Serve Stale on Upstream Error Fallback
        if cached_item and 'payload' in cached_item:
            logger.info(f"SERVING STALE DATA path={path} key={cache_key}")
            return build_response(200, json.loads(cached_item['payload']))

        # 4. Final Failure if no cached data exists at all
        logger.error(f"NO CACHE FALLBACK AVAILABLE path={path}")
        return build_response(502, {"error": "Upstream service unavailable"})