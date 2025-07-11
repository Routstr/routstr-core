#!/bin/bash

# Auto-update script for Docker Compose projects
# This script checks for new commits and updates Docker Compose when changes are detected

# Configuration - can be overridden by environment variables with sensible defaults
REPO_DIR="${REPO_DIR:-/home/ubuntu/proxy}"
LOG_FILE="${LOG_FILE:-/home/ubuntu/proxy/update.log}"
LOCK_FILE="${LOCK_FILE:-/tmp/proxy_update.lock}"
MAX_LOG_LINES="${MAX_LOG_LINES:-10000}"  # Maximum number of log lines to keep

# Detect which Docker Compose command is available
detect_docker_compose_cmd() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        echo "docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        echo "docker-compose"
    else
        log_message "ERROR: Neither 'docker compose' nor 'docker-compose' is available"
        exit 1
    fi
}

# Set the Docker Compose command
DOCKER_COMPOSE_CMD=$(detect_docker_compose_cmd)

# Function to log messages with timestamp
log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Function to prune log file to keep only recent entries
prune_logs() {
    if [ -f "$LOG_FILE" ]; then
        local line_count=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$line_count" -gt "$MAX_LOG_LINES" ]; then
            # Create a temporary file with only the last MAX_LOG_LINES lines
            tail -n "$MAX_LOG_LINES" "$LOG_FILE" > "${LOG_FILE}.tmp"
            mv "${LOG_FILE}.tmp" "$LOG_FILE"
            log_message "Log file pruned. Kept last $MAX_LOG_LINES lines."
        fi
    fi
}

# Function to cleanup on exit
cleanup() {
    rm -f "$LOCK_FILE"
}

# Set trap to cleanup on exit
trap cleanup EXIT

# Check if another instance is running
if [ -f "$LOCK_FILE" ]; then
    log_message "Another update process is already running. Exiting."
    exit 1
fi

# Create lock file
touch "$LOCK_FILE"

# Change to repository directory
cd "$REPO_DIR" || {
    log_message "ERROR: Cannot change to repository directory $REPO_DIR"
    exit 1
}

# Fetch latest changes from remote
log_message "Fetching latest changes from remote..."
git fetch origin 2>/dev/null

# Check if local branch is behind remote
LOCAL_HASH=$(git rev-parse HEAD)
REMOTE_HASH=$(git rev-parse origin/$(git branch --show-current))

if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
    log_message "New commits detected. Current: $LOCAL_HASH, Remote: $REMOTE_HASH"
    
    # Pull latest changes
    log_message "Pulling latest changes..."
    if git pull origin $(git branch --show-current) 2>&1 | tee -a "$LOG_FILE"; then
        log_message "Successfully pulled latest changes"
        
        # Stop current containers
        log_message "Stopping current containers..."
        sudo $DOCKER_COMPOSE_CMD down 2>&1 | tee -a "$LOG_FILE"
        
        # Build and start updated containers
        log_message "Building and starting updated containers..."
        if sudo $DOCKER_COMPOSE_CMD up -d --build 2>&1 | tee -a "$LOG_FILE"; then
            log_message "Successfully updated and restarted containers"
        else
            log_message "ERROR: Failed to start containers"
            exit 1
        fi
    else
        log_message "ERROR: Failed to pull changes"
        exit 1
    fi
else
    log_message "No new commits found. Repository is up to date."
fi

log_message "Update check completed successfully"

# Prune logs after each run to prevent unlimited growth
prune_logs
