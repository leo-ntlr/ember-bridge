#!/bin/bash
# Kept for the first night: everything now lives in ember.sh (same commands: status, stop, ...).
exec bash "$(dirname "$0")/ember.sh" "$@"
