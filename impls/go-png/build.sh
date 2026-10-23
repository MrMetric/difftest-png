#!/bin/sh
# Build the Go shim with whichever Go toolchain is available.
set -e
cd "$(dirname "$0")"
GO=$(command -v go 2>/dev/null || true)
[ -n "$GO" ] || GO="$HOME/.local/go-toolchain/go/bin/go"
GOTOOLCHAIN=local GOFLAGS=-trimpath exec "$GO" "$@"
