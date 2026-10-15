#!/bin/sh
exec python3 "$(dirname "$(realpath "$0")")/pi_sidebar.py" "$@"
