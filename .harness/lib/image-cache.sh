#!/usr/bin/env bash
# image_rebuild_needed DOCKERFILE MARKER [IMAGE_NAME]
# Prints "true" if the image should be rebuilt, "false" otherwise.
# When IMAGE_NAME is given, also rebuilds if the local image is gone
# (e.g., operator ran `docker rmi` while the marker still matches).
image_rebuild_needed() {
    local dockerfile="$1"
    local marker="$2"
    local image="${3:-}"

    [[ ! -f "$dockerfile" ]] && echo "true" && return 0
    [[ ! -f "$marker" ]]     && echo "true" && return 0

    if [[ -n "$image" ]] && ! image_exists "$image"; then
        echo "true"; return 0
    fi

    local current stored
    current=$(_harness_sha256 "$dockerfile") || { echo "true"; return 0; }
    stored=$(tr -d '[:space:]' < "$marker" 2>/dev/null) || { echo "true"; return 0; }

    [[ -z "$stored" ]]              && echo "true" && return 0
    [[ "$current" != "$stored" ]]   && echo "true" && return 0

    echo "false"
}

# save_image_hash DOCKERFILE MARKER
save_image_hash() {
    local dockerfile="$1"
    local marker="$2"
    _harness_sha256 "$dockerfile" > "$marker"
}

# image_exists IMAGE_NAME — exit 0 if the image exists locally.
image_exists() {
    local name="$1"
    [[ -n "$(docker images -q "$name" 2>/dev/null)" ]]
}

_harness_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}
