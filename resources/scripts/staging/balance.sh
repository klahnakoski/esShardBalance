#!/usr/bin/env bash

# FOR USE ON THE MANAGER MACHINE

chmod u+x balance.py

cd ~/esShardBalancer
export PYTHONPATH=.
python27 balance.py --settings=resources/config/staging/balance.json >& /dev/null < /dev/null &
disown -h
tail -n200 -f logs/balance.log
