#!/bin/bash
# G6 N+2: a greenboot REQUIRED check that always fails -- the faulty update of AFRL
# demo Step 5. It stands in for "the model service fails its health check": greenboot
# must count the failed boots and fall back to the previous deployment on its own.
echo "g6: this image (N+2) is broken on purpose -- required health check fails"
exit 1
