#!/usr/bin/env bash

# FOR USE ON THE MANAGER MACHINE

cd ~/esShardBalancer
export PYTHONPATH=.
python27 resources/scripts/balance.py --settings=resources/config/staging/balance.json
