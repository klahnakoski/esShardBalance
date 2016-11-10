#!/usr/bin/env bash

# FOR USE ON THE MANAGER MACHINE

cd ~/esShardBalancer
export PYTHONPATH=.
python27 balance.py --settings=resources/config/staging/balance.json >& /dev/null < /dev/null &
echo $! > run.pid
disown -h
tail -n200 -f logs/balance.log
