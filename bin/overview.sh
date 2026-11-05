#!/bin/sh
# Open the interactive graph view (serves web/dist, no runtime deps).
exec python3 -m omp_memory.web "$@"
