#!/usr/bin/env bash


cd ~/esShardBalancer
git pull origin better-balance
export PYTHONPATH=.

nohup python27 resources/scripts/es_fix_unassigned_shards.py --settings=resources/config/balance.json &
tail -n200 -f ./results/balance.log
