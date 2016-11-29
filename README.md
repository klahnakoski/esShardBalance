# esShardBalancer
Balance heterogeneous indexes using heterogeneous nodes in heterogeneous cluster zones

## Overview

Elasticsearch has naive shard balancing: It only works well when all indexes 
are the same size, when all nodes are the same size, and all zones are the 
same size.

The esShardBalancer disables the ES shard balancer, and adds new abilities: 

* Balance by node size - The shards of each index are spread evenly over the 
nodes in a cluster based on the amount of memory each node has.
* Indexes are given recovery priority - Highest priority indexes are recovered 
and rebalanced first.  Some data is more valuable because it used more, or it 
is harder to reindex.
* Zones can be labeled "risky" - This shard balancer will ensure there is at 
least one copy of each shard in a non-risky zone
* Zones can have different replica counts - Some zones have more resources 
than others, so more replicas can be stored there

These features allow us to run a big cluster, at a reasonable price, on a 
heterogeneous collection of AWS spot nodes.

## Important

ElasticSearch zone awareness and shard balancing must be turned off. 
esShardBalancer will attempt to do this for you at startup, but it is best
your config file has the following settings:

    cluster.routing.allocation.enable: none
    cluster.routing.allocation.awareness.attributes: 
    cluster.routing.allocation.awareness.force.zone.values:

Notice the `awareness` attribute are blank 

