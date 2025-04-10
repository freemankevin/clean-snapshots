import requests
from requests.auth import HTTPBasicAuth
import json
import re
from datetime import datetime
import logging
import time
import os
import sys
from typing import Dict, List, Optional, Tuple, Any
import schedule
from fastapi import FastAPI, Response
import uvicorn
import psutil

# --- Configuration from ENV ---
nexus_url = os.getenv('NEXUS_URL', 'http://nexus:8081')
username = os.getenv('NEXUS_USER', 'admin')
password = os.getenv('NEXUS_PASS', 'admin123')
repository = os.getenv('REPOSITORY_NAME', 'maven-snapshots')
retain_count = int(os.getenv('RETAIN_COUNT', '3'))
dry_run = os.getenv('DRY_RUN', 'false').lower() == 'true'
max_retries = int(os.getenv('MAX_RETRIES', '3'))
retry_delay = int(os.getenv('RETRY_DELAY', '5'))
log_level = os.getenv('LOG_LEVEL', 'INFO')
schedule_time = os.getenv('SCHEDULE_TIME', '03:00')
healthcheck_port = int(os.getenv('HEALTHCHECK_PORT', '8000'))

# Global state
last_run_time = None
last_run_status = "never_run"

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(log_level)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# File handler (persistent storage)
file_handler = logging.FileHandler('/var/log/nexus_cleanup.log')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Stream handler (for docker logs)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# Healthcheck app
app = FastAPI()

# Regex to capture timestamped SNAPSHOT versions
SNAPSHOT_PATTERN = re.compile(r"^(.*?)-(\d{8}\.\d{6})-(\d+)$")

class NexusAPIError(Exception):
    """Custom exception for Nexus API errors"""
    pass

# --- API Functions ---
def make_api_request(method: str, url: str, auth: HTTPBasicAuth, **kwargs) -> requests.Response:
    """Make an API request with retries and error handling."""
    session = requests.Session()
    session.auth = auth
    
    for attempt in range(max_retries):
        try:
            response = session.request(method, url, timeout=60, **kwargs)
            if response.status_code == 204:
                return response
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                continue
            raise NexusAPIError(f"API request failed after {max_retries} attempts: {str(e)}")

# --- Healthcheck Endpoints ---
@app.get("/health")
def health_check():
    """Comprehensive health check endpoint"""
    checks = {
        "storage": {
            "log_file_writable": os.access('/var/log/nexus_cleanup.log', os.W_OK),
            "disk_space": psutil.disk_usage('/').free > 100 * 1024 * 1024  # 100MB
        },
        "connectivity": {
            "nexus_reachable": check_nexus_connectivity()
        },
        "application": {
            "last_run_time": last_run_time,
            "last_run_status": last_run_status,
            "schedule": schedule_time
        }
    }
    
    is_healthy = all(all(category.values()) for category in checks.values())
    status_code = 200 if is_healthy else 503
    
    return Response(
        content=json.dumps({
            "status": "healthy" if is_healthy else "unhealthy",
            "checks": checks,
            "version": os.getenv("VERSION", "unknown"),
            "timestamp": datetime.now().isoformat()
        }),
        status_code=status_code,
        media_type="application/json"
    )

def check_nexus_connectivity() -> bool:
    """Check basic Nexus connectivity"""
    try:
        response = requests.get(
            f"{nexus_url}/service/rest/v1/status",
            auth=HTTPBasicAuth(username, password),
            timeout=5
        )
        return response.status_code == 200
    except Exception:
        return False

# --- Core Functions ---
def parse_snapshot_version(version_str: str) -> Optional[Tuple[str, datetime, int]]:
    """Parse timestamped SNAPSHOT version string."""
    match = SNAPSHOT_PATTERN.match(version_str)
    if match:
        base_version = match.group(1) + "-SNAPSHOT"
        timestamp_str = match.group(2)
        build_number = int(match.group(3))
        try:
            dt_obj = datetime.strptime(timestamp_str, '%Y%m%d.%H%M%S')
            return base_version, dt_obj, build_number
        except ValueError:
            return None
    return None

def get_all_components_paginated() -> Optional[List[Dict[str, Any]]]:
    """Get components using official /components endpoint with pagination."""
    all_components = []
    continuation_token = None
    url = f"{nexus_url}/service/rest/v1/components"
    auth = HTTPBasicAuth(username, password)
    
    logger.info("Fetching components (this may take time for large repositories)...")
    page_num = 1

    while True:
        params = {
            "repository": repository,
            "version": "*-SNAPSHOT"
        }
        
        if continuation_token:
            params["continuationToken"] = continuation_token

        try:
            logger.info(f"Fetching page {page_num}...")
            response = make_api_request("GET", url, auth, params=params)
            data = response.json()

            items = data.get("items", [])
            all_components.extend(items)
            logger.info(f"Found {len(items)} components on page {page_num}. Total: {len(all_components)}")

            continuation_token = data.get("continuationToken")
            if not continuation_token:
                logger.info("Reached end of paginated results.")
                break

            page_num += 1

        except (NexusAPIError, json.JSONDecodeError) as e:
            logger.error(f"Failed to fetch components: {str(e)}")
            return None

    logger.info(f"Finished fetching components. Total retrieved: {len(all_components)}")
    return all_components

def process_snapshots(components: List[Dict[str, Any]]) -> bool:
    """Group components and delete old SNAPSHOT versions."""
    if not components:
        return True

    artifacts: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    success = True

    # Group by (group, artifact name)
    for comp in components:
        key = (comp["group"], comp["name"])
        artifacts.setdefault(key, []).append(comp)

    logger.info(f"\nProcessing {len(artifacts)} unique artifacts...")

    for (group, name), comps in artifacts.items():
        logger.info(f"\nProcessing Artifact: {group}:{name}")

        version_branches: Dict[str, List[Dict[str, Any]]] = {}
        
        for comp in comps:
            parsed = parse_snapshot_version(comp["version"])
            if parsed:
                base_version, dt_obj, build_number = parsed
                version_branches.setdefault(base_version, []).append({
                    "component": comp,
                    "datetime": dt_obj,
                    "build": build_number
                })

        for base_version, snapshots in version_branches.items():
            logger.info(f"Processing SNAPSHOT branch: {base_version} ({len(snapshots)} versions)")
            
            snapshots.sort(key=lambda x: (x["datetime"], x["build"]), reverse=True)
            
            if len(snapshots) > retain_count:
                to_delete = snapshots[retain_count:]
                logger.info(f"Marking {len(to_delete)} for deletion.")
                
                for item in to_delete:
                    comp_to_delete = item["component"]
                    if not dry_run:
                        if not delete_component(comp_to_delete):
                            success = False

    return success

def delete_component(component: Dict[str, Any]) -> bool:
    """Delete component using official DELETE endpoint."""
    comp_id = component["id"]
    url = f"{nexus_url}/service/rest/v1/components/{comp_id}"
    auth = HTTPBasicAuth(username, password)
    
    try:
        response = make_api_request("DELETE", url, auth)
        if response.status_code == 204:
            logger.info(f"Successfully deleted {component['version']}")
            return True
        return False
    except NexusAPIError as e:
        logger.error(f"Delete failed: {str(e)}")
        return False

# --- Job Scheduling ---
def cleanup_job():
    """Scheduled job function"""
    global last_run_time, last_run_status
    start_time = time.time()
    last_run_time = datetime.now().isoformat()
    logger.info("Starting scheduled cleanup job")
    
    try:
        components = get_all_components_paginated()
        if components is not None:
            if process_snapshots(components):
                last_run_status = "success"
            else:
                last_run_status = "partial_failure"
        else:
            last_run_status = "failed"
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}", exc_info=True)
        last_run_status = "failed"
    finally:
        logger.info(f"Cleanup job completed in {time.time()-start_time:.2f}s (Status: {last_run_status})")

def run_scheduler():
    """Run the scheduled job"""
    if schedule_time.lower() == 'manual':
        logger.info("Running manual cleanup job")
        cleanup_job()
    else:
        logger.info(f"Scheduling daily cleanup at {schedule_time}")
        schedule.every().day.at(schedule_time).do(cleanup_job)
        
        while True:
            schedule.run_pending()
            time.sleep(60)

def main() -> None:
    """Main execution function"""
    # Start healthcheck server in background
    import threading
    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": app,
            "host": "0.0.0.0",
            "port": healthcheck_port,
            "log_level": "error"
        },
        daemon=True
    )
    server_thread.start()
    
    # Run the scheduler
    run_scheduler()

if __name__ == "__main__":
    main()