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

## Configuration

The `esShardBalnacer` messes with ElasticSearch zone awareness: Turning it off when shard placement breaks zone rules. Zone awareness is turn back on when it is finished a round of moves. There are two ways to ensure this works properly, both will use `IDENTICAL_NODE_ATTRIBUTE` constant.

#### Common node attribute

All nodes must have an common attribute/value pair:

    node.attr.cluster: myCluster

This is used to mark all nodes in a single zone, which effectively the same as turning off zone awareness.  If your nodes are already setup, you might be lucky to have a common attribute set already. 

    IDENTICAL_NODE_ATTRIBUTE = "xpack.installed"
    

#### Ensure awareness is turned off

If you are configuring new nodes, you ensure the zone awareness is off. Notice the `awareness` attributes are blank. 

    cluster.routing.allocation.enable: none
    cluster.routing.allocation.awareness.attributes: 
    cluster.routing.allocation.awareness.force.zone.values:

If awareness is off in the config files, then a common attribute/value is not required:

    IDENTICAL_NODE_ATTRIBUTE = ""
