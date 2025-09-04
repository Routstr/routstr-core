#!/usr/bin/env zsh

set -eE
set -o pipefail

error_handler() {
	local line_no=$1
	local exit_code=$2
	echo -e "\033[0;31mError in local_codespace script at line $line_no (exit code: $exit_code)\033[0m" >&2
	exit $exit_code
}

trap 'error_handler $LINENO $?' ERR

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

usage() {
	cat << EOF
Usage: local_codespace.sh [OPTIONS]

Create a local "codespace-like" Docker container and copy the current directory into it.

Options:
  -i, --image IMAGE        Base image (default: mcr.microsoft.com/devcontainers/base:ubuntu)
  -n, --name NAME          Container name (default: <cwd>-codespace-<timestamp>)
  -w, --workspace PATH     Workspace path inside container (default: /workspaces/<cwd>)
  -p, --port PORT          Publish port (repeatable). Examples: 8080 or 127.0.0.1:8080:8080
  -e, --env KEY=VALUE      Environment variable (repeatable)
  -s, --shell SHELL        Preferred shell inside container (default: /bin/bash)
      --rm                 Auto-remove container when it stops
      --tmux               Force opening a new tmux window attached to the container
      --no-tmux            Do not use tmux even if available
      --no-attach          Do not attach; just create container and copy files
  -h, --help               Show this help message

Examples:
  ./scripts/local_codespace.sh
  ./scripts/local_codespace.sh -i mcr.microsoft.com/devcontainers/base:debian -p 3000 -p 9229 \
      -e NODE_ENV=development --tmux
EOF
}

die() {
	echo -e "${YELLOW}$*${NC}" >&2
	exit 1
}

require_cmd() {
	command -v "$1" >/dev/null 2>&1 || die "Error: '$1' not found"
}

docker_ready() {
	docker info >/dev/null 2>&1 || die "Error: Docker daemon not running or not reachable"
}

sanitize_name() {
	local input="$1"
	local out
	out=$(echo "$input" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_.-]/-/g' | sed 's/--*/-/g' | cut -c1-63)
	if [ -z "$out" ]; then
		out="codespace-$(date +%m%d-%H%M)"
	fi
	echo "$out"
}

unique_container_name() {
	local base="$1"
	local name="$base"
	local idx=2
	while docker ps -a --format '{{.Names}}' | grep -q "^${name}$"; do
		name="${base}-${idx}"
		idx=$((idx+1))
		[ $idx -gt 99 ] && break
	done
	echo "$name"
}

# Defaults
IMAGE="${CODESPACE_IMAGE:-mcr.microsoft.com/devcontainers/base:ubuntu}"
PROJECT_DIR="$PWD"
PROJECT_BASENAME="${PROJECT_DIR:t}"
WORKSPACE_DIR="/workspaces/${PROJECT_BASENAME}"
CONTAINER_NAME=""
PREFERRED_SHELL="/bin/bash"
AUTO_REMOVE=false
ATTACH=true
TMUX_MODE="auto"  # on|off|auto

typeset -a PORTS
typeset -a ENVS
typeset -a EXTRA_ARGS

# Parse args
ARGS=()
while [[ $# -gt 0 ]]; do
	case "$1" in
		-i|--image)
			IMAGE="$2"; shift 2 ;;
		-n|--name)
			CONTAINER_NAME="$2"; shift 2 ;;
		-w|--workspace)
			WORKSPACE_DIR="$2"; shift 2 ;;
		-p|--port)
			PORTS+=("$2"); shift 2 ;;
		-e|--env)
			ENVS+=("$2"); shift 2 ;;
		-s|--shell)
			PREFERRED_SHELL="$2"; shift 2 ;;
		--rm)
			AUTO_REMOVE=true; shift ;;
		--tmux)
			TMUX_MODE="on"; shift ;;
		--no-tmux)
			TMUX_MODE="off"; shift ;;
		--no-attach)
			ATTACH=false; shift ;;
		-h|--help)
			usage; exit 0 ;;
		--)
			shift; break ;;
		-*)
			die "Unknown option: $1" ;;
		*)
			ARGS+=("$1"); shift ;;
	esac
done

require_cmd docker
require_cmd tar
docker_ready

if [ -z "$CONTAINER_NAME" ]; then
	CONTAINER_NAME="$(sanitize_name "${PROJECT_BASENAME}-codespace-$(date +%m%d-%H%M)")"
fi
CONTAINER_NAME="$(unique_container_name "$CONTAINER_NAME")"

echo -e "${GREEN}Using image:${NC} $IMAGE"
echo -e "${GREEN}Container name:${NC} $CONTAINER_NAME"
echo -e "${GREEN}Workspace dir:${NC} $WORKSPACE_DIR"

echo -e "${GREEN}Pulling image if needed...${NC}"
docker pull -q "$IMAGE" >/dev/null 2>&1 || true

typeset -a RUN_ARGS
RUN_ARGS=(-d -it --name "$CONTAINER_NAME" -w "$WORKSPACE_DIR" --label "local.codespace=1")

if [ "$AUTO_REMOVE" = true ]; then
	RUN_ARGS+=(--rm)
fi

for p in "${PORTS[@]}"; do
	RUN_ARGS+=(-p "$p")
done

for env_kv in "${ENVS[@]}"; do
	RUN_ARGS+=(-e "$env_kv")
done

for extra in "${EXTRA_ARGS[@]}"; do
	RUN_ARGS+=("$extra")
done

echo -e "${GREEN}Starting container...${NC}"
docker run "${RUN_ARGS[@]}" "$IMAGE" sh -c 'sleep infinity' >/dev/null

echo -e "${GREEN}Copying project files into container...${NC}"
docker exec "$CONTAINER_NAME" sh -c "mkdir -p '$WORKSPACE_DIR'"
tar -C "$PROJECT_DIR" -cf - . | docker exec -i "$CONTAINER_NAME" sh -c "tar -C '$WORKSPACE_DIR' -xf -"

# Choose interactive shell inside the container
SHELL_IN_CONTAINER="$PREFERRED_SHELL"
if ! docker exec "$CONTAINER_NAME" sh -c "command -v '$SHELL_IN_CONTAINER' >/dev/null 2>&1" >/dev/null 2>&1; then
	if docker exec "$CONTAINER_NAME" sh -c "command -v bash >/dev/null 2>&1" >/dev/null 2>&1; then
		SHELL_IN_CONTAINER="bash"
	elif docker exec "$CONTAINER_NAME" sh -c "command -v sh >/dev/null 2>&1" >/dev/null 2>&1; then
		SHELL_IN_CONTAINER="sh"
	else
		SHELL_IN_CONTAINER="/bin/sh"
	fi
fi

echo -e "${GREEN}Container is ready:${NC} $CONTAINER_NAME"
echo -e "${GREEN}To attach later:${NC} docker exec -it $CONTAINER_NAME $SHELL_IN_CONTAINER -c 'cd "$WORKSPACE_DIR"; exec $SHELL_IN_CONTAINER'"

if [ "$ATTACH" = true ]; then
	if [ "$TMUX_MODE" = "on" ] || { [ "$TMUX_MODE" = "auto" ] && [ -n "$TMUX" ]; }; then
		echo -e "${GREEN}Opening tmux window...${NC}"
		WINDOW_ID=$(tmux new-window -n "$CONTAINER_NAME" -c "$PROJECT_DIR" -P -F "#{window_id}" 2>&1 || true)
		if [[ "$WINDOW_ID" == *"@"* ]]; then
			# Likely a valid window id
			TMUX_CMD="docker exec -it $CONTAINER_NAME $SHELL_IN_CONTAINER -c 'cd \"$WORKSPACE_DIR\"; exec $SHELL_IN_CONTAINER'"
			sleep 0.2
			tmux send-keys -t "$WINDOW_ID" "$TMUX_CMD" C-m
			echo -e "${GREEN}✓ tmux window attached to container${NC}"
		else
			echo -e "${YELLOW}Warning: Could not create tmux window. Attaching in current terminal...${NC}" >&2
			exec docker exec -it "$CONTAINER_NAME" "$SHELL_IN_CONTAINER" -c "cd '$WORKSPACE_DIR'; exec $SHELL_IN_CONTAINER"
		fi
	else
		echo -e "${GREEN}Attaching to container shell...${NC}"
		exec docker exec -it "$CONTAINER_NAME" "$SHELL_IN_CONTAINER" -c "cd '$WORKSPACE_DIR'; exec $SHELL_IN_CONTAINER"
	fi
fi

echo -e "${GREEN}✓ Done. Container running in background: $CONTAINER_NAME${NC}"


