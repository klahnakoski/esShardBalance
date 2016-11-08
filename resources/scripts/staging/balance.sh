#!/usr/bin/env bash

# FOR USE ON THE MANAGER MACHINE

cd ~/esShardBalancer
export PYTHONPATH=.
nohup python27 balance.py --settings=resources/config/staging/balance.json &
echo $! > run.pid
disown -h
tail -n200 -f nohup.out
