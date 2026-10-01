#!/bin/sh
# Run the test suite. Set VZIP_SKIP_SLOW=1 to skip the >4 GiB sparse-file ZIP64 test.
cd "$(dirname "$0")/tests" && exec uv run --no-project --quiet --python 3.12 python -W ignore::ResourceWarning -m unittest discover -p 'test_*.py' "$@"
