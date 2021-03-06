
# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import, division, unicode_literals

import json
from collections import Mapping
from copy import copy

import boto
import boto.ec2
import boto.vpc
from fabric.api import settings as fabric_settings
from fabric.context_managers import hide
from fabric.operations import sudo
from fabric.state import env

import mo_json_config
import mo_math
from jx_python import jx
from mo_collections import UniqueIndex
from mo_dots import Data, FlatList, Null, coalesce, listwrap, literal_field, unwrap, wrap, wrap_leaves
from mo_future import text
from mo_http import http
from mo_json import json2value, value2json
from mo_logs import Log, constants, machine_metadata, startup, strings
from mo_math import MAX, MIN, SUM
from mo_math.randoms import Random
from mo_threads import Signal, Thread, Till, MAIN_THREAD
from mo_times import Date, Timer

DEBUG = True

CONCURRENT = 1  # NUMBER OF SHARDS TO MOVE CONCURRENTLY, PER NODE
BILLION = 1024 * 1024 * 1024
BIG_SHARD_SIZE = 2 * BILLION  # SIZE WHEN WE SHOULD BE MOVING ONLY ONE SHARD AT A TIME
MAX_MOVE_FAILURES = 3  # STOP TRYING TO MOVE

current_moving_shards = FlatList()  # BECAUSE ES WILL NOT TELL US WHERE THE SHARDS ARE MOVING TO

DEAD = "DEAD"
ALIVE = "ALIVE"
last_known_node_status = Data()
last_scrubbing = Data()

IDENTICAL_NODE_ATTRIBUTE = "xpack.installed"  # SOME node.attr[IDENTICAL_NODE_ATTRIBUTE] ALL THE SAME, REQUIRED FOR IMBALANCED SHARD ALLOCATION

ACCEPT_DATA_LOSS = False
ALLOCATE_REPLICA = "allocate_replica"
ALLOCATE_STALE_PRIMARY = "allocate_stale_primary"
ALLOCATE_EMPTY_PRIMARY = "allocate_empty_primary"


def assign_shards(settings):
    """
    ASSIGN THE UNASSIGNED SHARDS
    """
    path = settings.elasticsearch.host + ":" + text(settings.elasticsearch.port)
    # GET LIST OF NODES
    # coordinator    26.2gb
    # secondary     383.7gb
    # spot_47727B30   934gb
    # spot_BB7A8053   934gb
    # primary       638.8gb
    # spot_A9DB0988     5tb
    Log.note("get nodes")

    # stats = http.get_json(path+"/_stats")

    # TODO: PULL DATA ABOUT NODES TO INCLUDE THE USER DEFINED ZONES
    #

    zones = UniqueIndex("name")
    for z in settings.zones:
        z.num_nodes=0
        zones.add(z)

    stats = http.get_json(path+"/_nodes/stats")
    nodes = UniqueIndex("name", [
        {
            "name": n.name,
            "ip": n.host,
            "roles": n.roles,
            "zone": zones[n.attributes.zone],
            "memory": n.jvm.mem.heap_max_in_bytes,
            "disk": n.fs.total.total_in_bytes,
            "disk_free": n.fs.total.available_in_bytes
        }
        for k, n in stats.nodes.items()
    ])
    # if "primary" not in nodes or "secondary" not in nodes:
    #     Log.error("missing an important index\n{{nodes|json}}", nodes=nodes)

    risky_zone_names = set(z.name for z in settings.zones if z.risky)

    for node in nodes:
        node.zone.num_nodes+=1

    # USE SETTINGS TO OVERRIDE NODE PROPERTIES
    for n in settings.nodes:
        node = nodes[n.name]
        if node:
            for k, v in n.items():
                node[k] = v
            node.disk_free = MIN([node.disk_free, node.disk])

    # REVIEW NODE STATUS, AND ANY CHANGES
    first_run = not last_known_node_status
    for n in nodes:
        status, last_known_node_status[n.name] = last_known_node_status[n.name], ALIVE
        if status == DEAD:
            Log.warning("Node {{node}} came back to life!", node=n.name)
        elif status == None and not first_run:
            Log.alert("New node {{node}}!", node=n.name)

        if not n.zone:
            Log.error("Expecting all nodes to have a zone")
        if 'data' not in n.roles:
            n.disk = 0
            n.disk_free = 0
            n.memory = 0
    for n, status in last_known_node_status.copy().items():
        if not nodes[n] and status == ALIVE:
            Log.warning("Lost node {{node}}", node=n)
            last_known_node_status[n] = DEAD

    for _, siblings in jx.groupby(nodes, "zone.name"):
        siblings = wrap(filter(lambda n: 'data' in n.roles, siblings))
        for s in siblings:
            s.siblings = len(siblings)
            s.zone.memory = SUM(siblings.memory)

    Log.note("{{num}} nodes", num=len(nodes))

    # INDEX-LEVEL INFORMATION
    uuid_to_index_name = {i.uuid: i.index
        for i in convert_table_to_list(
            http.get(path + "/_cat/indices").content,
            ["status", "state", "index", "uuid", "_remainder"]
        )
    }

    # GET LIST OF SHARDS, WITH STATUS
    # debug20150915_172538                0  p STARTED        37319   9.6mb 172.31.0.196 primary
    # debug20150915_172538                0  r UNASSIGNED
    # debug20150915_172538                1  p STARTED        37624   9.6mb 172.31.0.39  secondary
    # debug20150915_172538                1  r UNASSIGNED
    shards = wrap(list(convert_table_to_list(
        http.get(path + "/_cat/shards").content,
        ["index", "i", "type", "status", "num", "size", "ip", "node"]
    )))
    current_moving_shards.__clear__()
    for s in shards:
        s.i = int(s.i)
        s.size = text_to_bytes(s.size)
        if s.node.find(" -> ") != -1:
            m = s.node.split(" -> ")
            s.node = m[0]  # <from> " -> " <to> format
            destination = m[1].split(" ")[-1]
            if nodes[destination]:
                destination = nodes[destination]
            else:
                for n in nodes:
                    if n.ip == destination:
                        destination = n
                        break

            current_moving_shards.append({
                "index": s.index,
                "shard": s.i,
                "from_node": m[0],
                "to_node": destination.name
            })
            # shards.append(set_default({"node": destination}, s))
        s.node = nodes[s.node]

    Log.note("TOTAL SHARDS: {{num}}", num=len(shards))
    Log.note("{{num}} shards moving", num=len(current_moving_shards))

    # TODO: MAKE ZONE OBJECTS TO STORE THE NUMBER OF REPLICAS

    # ASSIGN SIZE TO ALL SHARDS
    red_shards = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        if all(r.status == "UNASSIGNED" for r in replicas):
            red_shards.append(g)
        size = MAX(replicas.size)
        for r in replicas:
            r.size = size

    relocating = wrap([s for s in shards if s.status in ("RELOCATING", "INITIALIZING")])
    Log.note("{{num}} shards allocating", num=len(relocating))

    for m in copy(current_moving_shards):
        for s in shards:
            if s.index == m.index and s.i == m.shard and s.node.name == m.to_node and s.status == "STARTED":
                # FINISHED MOVE
                current_moving_shards.remove(m)
                break
            elif s.index == m.index and s.i == m.shard and s.node.name == m.from_node and s.status == "RELOCATING":
                # STILL MOVING, ADD A VIRTUAL SHARD TO REPRESENT THE DESTINATION OF RELOCATION
                s = copy(s)
                s.type = 'r'
                s.node = nodes[m.to_node]
                s.status = "INITIALIZING"
                if s.node:  # HAPPENS WHEN SENDING SHARD TO UNKNOWN
                    relocating.append(s)
                    shards.append(s)  # SORRY, BUT MOVING SHARDS TAKE TWO SPOTS
                break
        else:
            # COULD NOT BE FOUND
            current_moving_shards.remove(m)

    # if red_shards:
    #     Log.warning("Cluster is RED")
    #     # DO NOT SCRUB WHEN WE ARE MISSING SHARDS
    #     # ALLOCATE SHARDS INSTEAD
    #     find_and_allocate_shards(nodes, uuid_to_index_name, settings, red_shards)
    # else:
    #     # SCRUB THE NODE DIRECTORIES SO THERE IS ROOM
    #     clean_out_unused_shards(nodes, shards, uuid_to_index_name, settings)

    # AN "ALLOCATION" IS THE SET OF SHARDS FOR ONE INDEX ON ONE NODE
    # CALCULATE HOW MANY SHARDS SHOULD BE IN EACH ALLOCATION
    allocation = UniqueIndex(["index", "node.name"])
    replicas_per_zone = {}  # MAP <index> -> <zone.name> -> #shards

    for g, replicas in jx.groupby(shards, "index"):
        Log.note("review replicas of {{index}}", index=g.index)
        num_primaries = len(filter(lambda r: r.type == 'p', replicas))

        for zone in zones:
            override = wrap([i for i in settings.allocate if (i.name == g.index or (i.name.endswith("*") and g.index.startswith(i.name[:-1]))) and i.zone == zone.name])[0]
            if override:
                wrap(replicas_per_zone)[literal_field(g.index)][literal_field(zone.name)] = MIN([coalesce(override.shards, zone.shards), zone.num_nodes])
            else:
                wrap(replicas_per_zone)[literal_field(g.index)][literal_field(zone.name)] = zone.shards

        num_replicas = sum(replicas_per_zone[g.index].values())
        if mo_math.round(float(len(replicas)) / float(num_primaries), decimal=0) != num_replicas:
            # DECREASE NUMBER OF REQUIRED REPLICAS
            # MAY NOT BE NEEDED BECAUSE WE NOW ARE ABLE TO FORCE ALLOCATE SHARDS
            # response = http.put(
            #     path + "/" + g.index + "/_settings",
            #     json={"index.recovery.initial_shards": 1}
            # )
            # Log.note("Number of shards required {{index}}\n{{result}}", index=g.index, result=json2value(utf82unicode(response.content)))

            # CHANGE NUMBER OF REPLICAS
            response = http.put(path + "/" + g.index + "/_settings", json={"index": {"number_of_replicas": num_replicas-1}})
            Log.note(
                "Update to {{num}} replicas for {{index}}\n{{result}}",
                num=num_replicas,
                index=g.index,
                result=json2value(response.content.decode('utf8'))
            )

        for n in nodes:
            if 'data' in n.roles:
                pro = (float(n.memory) / float(n.zone.memory)) * (replicas_per_zone[g.index][n.zone.name] * num_primaries)
                min_allowed = mo_math.floor(pro)
                max_allowed = mo_math.ceiling(pro) if n.memory else 0
            else:
                min_allowed = 0
                max_allowed = 0

            shards_in_node = list(filter(lambda r: r.node.name == n.name, replicas))
            allocate_ = {
                "index": g.index,
                "node": n,
                "min_allowed": min_allowed,
                "max_allowed": max_allowed,
                "shards": shards_in_node
            }
            for sh in shards_in_node:
                sh.allocate = allocate_  # ACTIVE SHARDS WILL HAVE ACCESS TO allocate

            allocation.add(allocate_)

        index_size = SUM(replicas.size)
        for r in replicas:
            r.index_size = index_size
            r.siblings = num_primaries

    del ALLOCATION_REQUESTS[:]

    # LOOKING FOR SHARDS WITH ZERO STARTED INSTANCES
    not_started = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        started_replicas = list(set([s.zone.name for s in replicas if s.status in {"STARTED", "RELOCATING", "INITIALIZING"}]))
        if len(started_replicas) == 0:
            # MARK NODE AS RISKY
            for s in replicas:
                if s.status == "UNASSIGNED":
                    not_started.append(s)
                    break  # ONLY NEED ONE
    if not_started:
        # TODO: CANCEL ANYTHING MOVING IN SPOT
        Log.warning("{{num}} shards have not started", num=len(not_started))
        # Log.warning("Shards not started!!\n{{shards|json|indent}}", shards=not_started)
        initailizing_indexes = set(relocating.index)
        busy = [n for n in not_started if n.index in initailizing_indexes]
        please_initialize = [n for n in not_started if n.index not in initailizing_indexes]
        if len(busy) > 1:
            # WE GET HERE WHEN AN IMPORTANT NODE IS WARMING UP ITS SHARDS
            # SINCE WE CAN NOT RECOGNIZE THE ASSIGNMENT THAT WE MAY HAVE REQUESTED LAST ITERATION
            Log.note("Delay work, cluster busy RELOCATING/INITIALIZING {{num}} shards", num=len(relocating))
            return
        # TODO: Some indexes have no safe zone, so `- risky_zone_names` is a bad strategy
        Log.note("{{num}} shards have not started", num=len(please_initialize))
        allocate(30, please_initialize, set(n.zone.name for n in nodes) - risky_zone_names, "not started", 1, settings)
    else:
        Log.note("All shards have started")

    # LOOKING FOR SHARDS WITH ONLY ONE INSTANCE, IN THE RISKY ZONES
    high_risk_shards = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        # TODO: CANCEL ANYTHING MOVING IN SPOT
        realized_zone_names = set([s.node.zone.name for s in replicas if s.status in {"STARTED", "RELOCATING"}])
        if len(realized_zone_names-risky_zone_names) == 0:
            # MARK NODE AS RISKY
            for s in replicas:
                if s.status == "UNASSIGNED":
                    high_risk_shards.append(s)
                    break  # ONLY NEED ONE
                # else:  # THIS IS NOT GOOD BECAUSE MOVING DUPLICATED SHARDS IS LOWER PRIORITY THAN ASSIGNING REPLICAS
                #     # PICK ONE
                #     high_risk_shards.append(Random.sample([r for r in replicas if r.type == 'r'], 1)[0])
    if high_risk_shards:
        # TODO: Some indexes have no safe zone, so `- risky_zone_names` is a bad strategy
        Log.note("{{num}} high risk shards found", num=len(high_risk_shards))

        low_risk_zones = {}
        high_risk_zones = {}
        for s in high_risk_shards:
            zones_for_shard = set([z for z, c in replicas_per_zone[s.index].items() if c > 0])
            if zones_for_shard - risky_zone_names:
                low_risk_zones.setdefault(tuple(sorted(zones_for_shard - risky_zone_names)), []).append(s)
            high_risk_zones.setdefault(tuple(sorted(zones_for_shard & risky_zone_names)), []).append(s)

        for z, hrs in low_risk_zones.items():
            allocate(10, hrs, z, "high risk shards", 2, settings)

        for z, hrs in high_risk_zones.items():
            allocate(10, hrs, z, "high risk shards (alt)", 2.1, settings)

        # allocate(10, high_risk_shards, set(n.zone.name for n in nodes) - risky_zone_names, "high risk shards", 2, settings)
    else:
        Log.note("No high risk shards found")

    # THIS HAPPENS WHEN THE ES SHARD LOGIC ASSIGNED TOO MANY REPLICAS TO A SINGLE ZONE
    overloaded_zone_index_pairs = set()
    over_allocated_shards = Data()
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        for z in zones:
            realized_replicas = filter(lambda r: r.status == "STARTED" and r.node.zone.name == z.name, replicas)
            expected_replicas = replicas_per_zone[g.index][z.name]
            if len(realized_replicas) > expected_replicas:
                overloaded_zone_index_pairs.add((z.name, g.index))
                # IS THERE A PLACE TO PUT IT?
                best_zone = None
                for possible_zone in zones:
                    allowed_shards = replicas_per_zone[g.index][possible_zone.name]
                    current_shards = len(filter(
                        lambda r: r.status in {"INITIALIZING", "STARTED", "RELOCATING"} and r.node.zone.name == possible_zone.name,
                        replicas
                    ))
                    if not best_zone or (not best_zone[0].risky and z.risky) or (best_zone[0].risky == z.risky and best_zone[1] > current_shards):
                        best_zone = possible_zone, current_shards
                    if allowed_shards > current_shards:
                        # TODO: NEED BETTER CHOOSER; NODE WITH MOST SHARDS?

                        try:
                            i = Random.weight([
                                # DO NOT ASSIGN PRIMARY SHARDS TO BUSY ZONES
                                r.siblings if not possible_zone.busy or (r.type != 'p') else 0
                                for r in realized_replicas
                            ])
                            shard = realized_replicas[i]
                            over_allocated_shards[possible_zone.name] += [shard]
                        except ZeroDivisionError as z:
                            Log.note("could not rebalance {{g}}", g=g)
                        except Exception as e:
                            Log.note("could not rebalance {{g}}", g=g)
                        break
                else:
                    if z == best_zone[0]:
                        continue
                    i = Random.weight([
                        # DO NOT ASSIGN PRIMARY SHARDS TO BUSY ZONES
                        r.siblings if bool(best_zone[0].busy) == (r.type != 'p') else 0
                        for r in realized_replicas
                    ])
                    shard = realized_replicas[i]
                    # alloc = allocation[g.index, shard.node.name]
                    potential_peers =filter(
                        lambda r: r.status in {"INITIALIZING", "STARTED", "RELOCATING"} and r.index ==shard.index and r.i==shard.i and r.node.zone==shard.node.zone,
                        shards
                    )
                    if len(potential_peers) >= best_zone[0].shards:
                        continue
                    over_allocated_shards[best_zone[0].name] += [shard]

    if over_allocated_shards:
        for z, v in over_allocated_shards.items():
            Log.note("{{num}} shards can be moved to {{zone}}", num=len(v), zone=z)
            allocate(CONCURRENT, v, {z}, "over allocated", 3, settings)
    else:
        Log.note("No over-allocated shard found")

    # MOVE SHARDS OUT OF FULL NODES (BIGGEST TO SMALLEST)
    free_space = Data()  # MAP FROM ZONENAME TO SHARDS TO MOVE
    for n in nodes:
        if n.disk and float(n.disk_free) / float(n.disk) < 0.05:
            biggest_shard = jx.sort([s for s in shards if s.node == n], "size").last()
            if biggest_shard.status == "STARTED":
                free_space[n.zone.name] += [biggest_shard]
            else:
                pass  # TRY AGAIN LATER
    if free_space:
        for z, moves in free_space.items():
            Log.note("{{num}} shards can be moved to free up space in {{zone}}", num=len(moves), zone=z)
            allocate(CONCURRENT, moves, {z}, "free space", 3, settings)

    # MOVE PRIMARY OFF busy ZONE
    move_primaries = Data()
    current_index = "not an index"
    is_latest = True
    for g, replicas in jx.reverse(list(jx.groupby(shards, ["index", "i"]))):
        # PRIORITY TO MOST RECENT INDEX
        if g.index != current_index:
            if len(current_index)>15 and len(g.index)>15 and current_index[0:-15] == g.index[0:-15]:
                # MORE INDEXES OF SAME ALIAS
                is_latest = False
                continue
            else:
                current_index = g.index
                is_latest = True

        # FOR NOW, ONLY MOVE LATEST INDEX IN SERIES
        if is_latest and all(s == 'STARTED' for s in replicas.status):
            for is_busy_replica in replicas:
                if is_busy_replica.node.zone.busy and is_busy_replica.type == 'p':
                    candidates = [
                        rr
                        for rr in replicas
                        if rr is not is_busy_replica and not rr.node.zone.busy
                    ]
                    if candidates:  # SOMETIMES ALL SHARDS ARE IN busy ZONE
                        other = Random.sample(candidates, 1)[0]
                        # SWAP
                        move_primaries[is_busy_replica.node.zone.name] += [is_busy_replica]
                        allocate(CONCURRENT, [other], {is_busy_replica.node.zone.name}, "move replica into busy zone", 3, settings)
    if move_primaries:
        for zone_name, assign in move_primaries.items():
            Log.note("{{num}} primary shards can be moved to less busy zone", num=len(assign))
    else:
        Log.note("No primary shards in busy zone")

    # LOOK FOR DUPLICATION OPPORTUNITIES
    # ONLY DUPLICATE PRIMARY SHARDS AT THIS TIME
    # IN THEORY THIS IS FASTER BECAUSE THEY ARE IN THE SAME ZONE (AND BETTER MACHINES)
    dup_shards = Data()
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        # WE CAN ASSIGN THIS REPLICA WITHIN THE SAME ZONE
        for s in replicas:
            if s.status != "UNASSIGNED" or s.type != "p":
                continue
            for z in settings.zones:
                started_count = len([r for r in replicas if r.status in {"STARTED"} and r.node.zone.name == z.name])
                active_count = len([r for r in replicas if r.status in {"INITIALIZING", "STARTED", "RELOCATING"} and r.node.zone.name == z.name])
                if started_count >= 1 and active_count < replicas_per_zone[g.index][z.name]:
                    dup_shards[z.name] += [s]
            break  # ONLY ONE SHARD PER CYCLE

    if dup_shards:
        for zone_name, assign in dup_shards.items():
            # Log.note("{{dups}}", dups=assign)
            Log.note("{{num}} shards can be duplicated in the {{zone}} zone", num=len(assign), zone=zone_name)
            allocate(CONCURRENT, assign, {zone_name}, "duplicate shards", 5, settings)
    else:
        Log.note("No intra-zone duplication remaining")

    # LOOK FOR UNALLOCATED SHARDS
    low_risk_shards = Data()
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        # WE CAN ASSIGN THIS REPLICA TO spot
        for s in replicas:
            if s.status != "UNASSIGNED":
                continue
            for z in settings.zones:
                active_count = len([r for r in replicas if r.status in {"INITIALIZING", "STARTED", "RELOCATING"} and r.node.zone.name == z.name])
                if active_count < replicas_per_zone[g.index][z.name]:
                    low_risk_shards[z.name] += [s]
            break  # ONLY ONE SHARD PER CYCLE

    if low_risk_shards:
        for zone_name, assign in low_risk_shards.items():
            Log.note("{{num}} low risk shards can be assigned to {{zone}} zone", num=len(assign), zone=zone_name)
            allocate(CONCURRENT, assign, {zone_name}, "low risk shards", 4, settings)
    else:
        Log.note("No low risk shards found")

    # LOOK FOR SHARD IMBALANCE
    rebalance_candidates = Data()
    for g, replicas in jx.groupby(filter(lambda r: r.status == "STARTED", shards), ["node.name", "index"]):
        g = wrap_leaves(g)
        replicas = list(replicas)
        if not g.node:
            continue
        _node = nodes[g.node.name]
        alloc = allocation[g]
        if (_node.zone.name, g.index) in overloaded_zone_index_pairs:
            continue
        for i in range(alloc.max_allowed, len(replicas), 1):
            candidates = [
                r
                for r in replicas
                # DO NOT MOVE PRIMARIES TO buzy ZONES
                if not _node.zone.busy or r.type != 'p'
            ]
            if candidates:
                shard = Random.sample(candidates, 1)[0]
                replicas.remove(shard)
                rebalance_candidates[_node.zone.name] += [shard]

    if rebalance_candidates:
        for z, b in rebalance_candidates.items():
            Log.note("{{num}} shards can be moved to better location within {{zone|quote}} zone", zone=z, num=len(b))
            allocate(CONCURRENT, b, {z}, "not balanced", 4, settings)
    else:
        Log.note("No shards need to be balanced")

    # LOOK FOR OTHER, SLOWER, DUPLICATION OPPORTUNITIES
    dup_shards = Data()
    for _, replicas in jx.groupby(shards, ["index", "i"]):
        # WE CAN ASSIGN THIS REPLICA WITHIN THE SAME ZONE
        for s in replicas:
            if s.status != "UNASSIGNED":
                continue
            for z in settings.zones:
                started_count = len([r for r in replicas if r.status in {"STARTED"} and r.node.zone.name==z.name])
                active_count = len([r for r in replicas if r.status in {"INITIALIZING", "STARTED", "RELOCATING"} and r.node.zone.name==z.name])
                if started_count >= 1 and active_count < z.shards:
                    dup_shards[z.name] += [s]
            break  # ONLY ONE SHARD PER CYCLE

    if dup_shards:
        for zone_name, assign in dup_shards.items():
            # Log.note("{{dups}}", dups=assign)
            Log.note("{{num}} shards can be duplicated between zones", num=len(assign))
            allocate(CONCURRENT, assign, {zone_name}, "inter-zone duplicate shards", 7, settings)
    else:
        Log.note("No inter-zone duplication remaining")

    # ENSURE ALL NODES HAVE THE MINIMUM NUMBER OF SHARDS
    #
    # Problem of 3 nodes AND 7 shards: Any node can have up to three shards,
    # so is (3, 3, 1) a legitimate configuration? It is better to slightly better
    # balance to (3, 2, 2).
    #
    # WE ONLY DO THIS IF THERE IS NOT OTHER REBALANCING TO BE DONE, OTHERWISE
    # IT WILL ALTERNATE SHARDS (CONTINUALLY TRYING TO FILL SPACE, BUT MAKING A HOLE ELSEWHERE)
    total_moves = 0
    for index_name in set(shards.index):
        for z in set([n.zone.name for n in nodes]):
            if not rebalance_candidates[z]:
                rebalance_candidate = None  # MOVE ONLY ONE SHARD, PER INDEX, PER ZONE, AT A TIME
                most_shards = 0  # WE WANT TO OFFLOAD THE NODE WITH THE MOST SHARDS
                destination_zone_name = None

                for n in nodes:
                    if n.zone.name != z:
                        continue

                    alloc = allocation[index_name, n.name]
                    if (n.name, index_name) in overloaded_zone_index_pairs:
                        continue
                    if not alloc.shards or len(alloc.shards) < alloc.min_allowed:
                        destination_zone_name = z
                        continue
                    started_shards = [r for r in alloc.shards if r.status in {"STARTED"}]
                    if most_shards >= len(started_shards):
                        continue

                    if MAX([1, alloc.min_allowed]) < len(started_shards):
                        shard = started_shards[0]
                        rebalance_candidate = shard
                        most_shards = len(started_shards)

                if destination_zone_name and rebalance_candidate:
                    total_moves += 1
                    allocate(CONCURRENT, [rebalance_candidate], {destination_zone_name}, "slightly better balance", 8, settings)
    if total_moves:
        Log.note(
            "{{num}} shards can be moved to slightly better location within their own zone",
            num=total_moves,
        )

    try:
        _allocate(relocating, path, nodes, shards, red_shards, allocation, settings)
    finally:
        enable_zone_restrictions(path)

local_ip_to_public_ip_map = Null


def get_ip_map():
    global local_ip_to_public_ip_map
    if local_ip_to_public_ip_map:
        return
    Log.note("getting public/private ip map")
    param = mo_json_config.get("file://~/private.json#aws_credentials")
    param = dict(
        region_name=param.region,
        aws_access_key_id=unwrap(param.aws_access_key_id),  # TRUE None REQUIRED
        aws_secret_access_key=unwrap(param.aws_secret_access_key)  # TRUE None REQUIRED
    )
    ec2_conn = boto.ec2.connect_to_region(**param)
    reservations = ec2_conn.get_all_instances()
    local_ip_to_public_ip_map = {
        ii.private_ip_address: i.ip_address
        for r in reservations
        for i in r.instances
        for ii in i.interfaces
    }


def find_and_allocate_shards(nodes, uuid_to_index_name, settings, red_shards):
    red_shards = [(g.index, g.i) for g in red_shards]

    # PICK NON-RISKY NODES FIRST
    for node in jx.sort(list(nodes), "zone.risky"):
        Log.note("review {{node}}", node=node.name)
        for d in get_node_directories(node, uuid_to_index_name, settings):
            if (d.index, d.i) not in red_shards:
                continue

            command = wrap({ALLOCATE_STALE_PRIMARY: {
                "accept_data_loss": ACCEPT_DATA_LOSS,
                "index": d.index,
                "shard": d.i,
                "node": node.name  # nodes[i].name
            }})

            if ACCEPT_DATA_LOSS:
                Log.warning(
                    "{{motivation}}: {{mode|upper}} index={{shard.index}}, shard={{shard.i}}, type={{shard.type}}, assign_to={{node}}",
                    motivation="Primary shard assign to known directory with data loss",
                    shard=d,
                    node=node.name
                )
            else:
                Log.note(
                    "{{motivation}}: {{mode|upper}} index={{shard.index}}, shard={{shard.i}}, type={{shard.type}}, assign_to={{node}}",
                    motivation="Primary shard assign to known directory",
                    shard=d,
                    node=node.name
                )

            path = settings.elasticsearch.host + ":" + text(settings.elasticsearch.port)
            response = http.post(path + "/_cluster/reroute", json={"commands": [command]})
            result = json2value(response.content.decode('utf8'))
            if response.status_code not in [200, 201] or not result.acknowledged:
                if isinstance(result.error, Mapping):
                    main_reason = result.error.root_cause.reason
                else:
                    main_reason = strings.between(result.error, "[NO", "]")

                if main_reason == None:
                    Log.note("Failure for unknwon reason")
                elif "shard cannot be allocated on same node" in main_reason:
                    Log.note("ok: ES automatically initialized already")
                elif main_reason and main_reason.find("too many shards on nodes for attribute") != -1:
                    pass  # THIS WILL HAPPEN WHEN THE ES SHARD BALANCER IS ACTIVATED, NOTHING WE CAN DO
                    Log.note("failed: zone full")
                elif main_reason and main_reason.find("after allocation more than allowed") != -1:
                    pass
                    Log.note("failed: out of space")
                elif "failed to resolve [" in result.error:
                    # LOST A NODE WHILE SENDING UPDATES
                    lost_node_name = strings.between(result.error, "failed to resolve [", "]").strip()
                    Log.warning("Lost node during allocate {{node}}", node=lost_node_name)
                    nodes[lost_node_name].zone = None
                else:
                    Log.warning(
                        "{{code}} Can not move/allocate:\n\treason={{reason}}\n\tdetails={{error|quote}}",
                        code=response.status_code,
                        reason=main_reason,
                        error=result.error
                    )
            else:
                Log.note(
                    "ok={{result.acknowledged}}",
                    result=result
                )


def get_node_directories(node, uuid_to_index_name, settings):
    """
    :param node:
    :param settings:
    :return: LIST OF SHARDS AND THEIR DIRECTORIES
    """

    # FIND THE IP
    IP = node.ip
    if not machine_metadata.aws_instance_type:
        get_ip_map()
        IP = local_ip_to_public_ip_map.get(node.ip, node.ip)
    if not IP:
        Log.error("Expecting an ip address for {{node}}", node=node.name)

    if IP == '52.37.182.91':  # SKIP TUID SERVER
        Log.note("Hardcoded: Skipping TUID server at 52.37.182.91")
        return Null

    Log.note("using ip {{ip}}", ip=IP)

    # SETUP FABRIC
    for k, v in settings.connect.items():
        env[k] = v
    env.host_string = IP
    env.abort_exception = Log.error

    # LOGIN TO FIND SHARDS
    try:
        with fabric_settings(warn_only=True):
            with hide('output'):
                directories = sudo("find /data* -type d")
                drive_space = sudo("df -h")
    except Exception as e:
        Log.warning("Can not get directories!", cause=e)
        return Null
    # /data1/active-data/nodes/0/indices/jobs20161001_000000
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/11
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/11/_state
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/11/translog
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/11/index
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/6
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/6/_state
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/6/translog
    # /data1/active-data/nodes/0/indices/jobs20161001_000000/6/index
    # CATCH THE OUT-OF-CONTROL LOGGING THAT FILLS DRIVES (AND OTHER NASTINESS)
    for line in drive_space.split("\n"):
        fullness = strings.between(line, " ", "%")
        if mo_math.is_integer(fullness) and int(fullness) >= 98:
            Log.warning("Drive at {{ip}} has full drive {{drive|quote}}", ip=IP, drive=line)

    output = FlatList()
    for dir_ in directories.split("\n"):
        dir_ = dir_.strip()
        if dir_.endswith("No such file or directory"):
            continue
        path = dir_.split("/")
        if len(path) != 7:
            continue
        index = uuid_to_index_name.get(path[5])
        if not index:
            Log.warning("not expected dir={{dir}} for machine {{ip}}", dir=dir_, ip=IP)
            continue  # SOMETIMES THERE ARE JUNK DIRECTORIES
        if path[6] == "_state":
            continue  # SOMETIMES THERE ARE _state DIRECTORIES
        try:
            shard = int(path[6])
            output.append({
                "index": index,
                "i": shard,
                "dir": dir_
            })
        except Exception as e:
            Log.error("not expected dir={{dir}} for machine {{ip}}", cause=e, dir=dir_, ip=IP)
    return output


def clean_out_unused_shards(nodes, shards, uuid_to_index_name, settings):
    if settings.disable_cleaner:
        return
    for node in nodes:
        try:
            cleaned = _clean_out_one_node(node, shards, uuid_to_index_name, settings)
            if cleaned:
                break  # EXIT EARLY SO WE CAN GET TO THE JOB OF BALANCING
        except Exception as e:
            Log.warning("can not clear {{node}}", node=node.name, cause=e)


def _clean_out_one_node(node, all_shards, uuid_to_index_name, settings):
    # if not node.name.startswith("spot"):
    #     return
    if last_scrubbing[node.name] > Date("now-12hour"):
        return False  # NO WORK DONE
    last_scrubbing[node.name] = Date.now()

    expected_shards = [
        (r.index, r.i)
        for r, _ in jx.groupby(jx.filter(all_shards, {"eq": {"node.name": node.name}}), ["index", "i"])
    ]

    please_remove = []
    for d in get_node_directories(node, uuid_to_index_name, settings):
        if (d.index, d.i) in expected_shards:
            continue

        with hide('output'):
            young_files = text(sudo("find "+d.dir+" -cmin -120 -type f"))
            if young_files:
                Log.error("attempt to remove young files")
            else:
                please_remove.append(d.dir)

    for dir_ in please_remove:
        Log.note("Scrubbing node {{node}}: Remove {{path}}", node=node.name, path=dir_)
        with hide('output'):
            sudo("rm -fr " + dir_)

    return bool(please_remove)


ALLOCATION_REQUESTS = []


def allocate(concurrent, proposed_shards, zones, reason, mode_priority, settings):
    if DEBUG:
        assert all(isinstance(z, text) for z in zones)
    for s in proposed_shards:
        move = {
            "shard": s,
            "to_zone": zones,
            "concurrent": concurrent,
            "reason": reason,
            "mode_priority": mode_priority,
            "replication_priority": replication_priority(s, settings)
        }
        ALLOCATION_REQUESTS.append(move)


def replication_priority(shard, settings):
    for i, prefix in enumerate(settings.replication_priority):
        if prefix.endswith("*") and shard.index.startswith(prefix[:-1]):
            return i
        elif shard.index == prefix:
            return i
    return len(settings.replication_priority)


def net_shards_to_move(concurrent, shards, relocating):
    sorted_shards = jx.sort(shards, ["index_size", "size"])
    total_size = 0
    for s in sorted_shards:
        if total_size > BIG_SHARD_SIZE:
            break
        concurrent += 1
        total_size += s.size
    concurrent = max(concurrent, CONCURRENT)
    net = concurrent - len(relocating)
    return net, sorted_shards


def _allocate(relocating, path, nodes, all_shards, red_shards, allocation, settings):
    moves = jx.sort(ALLOCATION_REQUESTS, ["mode_priority", "replication_priority", "shard.index_size", "shard.i"])

    inbound_data = outbound_data = Data()  # TODO: SEE IF THIS IS TOO SLOW: NODE ALLOWED INGRESS OR EGRESS, NOT BOTH
    for s in relocating:
        if s.status == "INITIALIZING":
            primaries = [
                p
                for p in all_shards
                if p.index == s.index and p.status == "STARTED" and p.type == 'p' and p.i == s.i
            ]
            if primaries:
                # PRIMARY SHARD IS USED TO INITIALIZE SHARD
                source_node = primaries[0].node.name if primaries else None
                outbound_data[literal_field(source_node)] += s.size

            inbound_data[literal_field(s.node.name)] += s.size
        elif s.status == "RELOCATING":
            # WE ALREADY ADDED A VIRTUAL INITIALIZING SHARD TO CATCH inbound_data
            outbound_data[literal_field(s.node.name)] += s.size

    Log.note(
        "Busy nodes:\n{{nodes|json|indent}}",
        nodes={k: text(mo_math.round(v / (1000 * 1000 * 1000), digits=3)) + "G" for k, v in outbound_data.items()}
    )

    done = set()  # (index, i) pair
    move_failures = 0
    sent_full_nodes_warning = False
    Log.note("Considering {{num}} moves", num=len(moves))
    for move in moves:
        shard = move.shard
        if (shard.index, shard.i) in done:
            continue
        source_node = shard.node.name

        if not source_node:
            primaries = [
                p
                for p in all_shards
                if p.index == shard.index and p.status == "STARTED" and p.type == 'p' and p.i == shard.i
            ]
            source_node = primaries[0].node.name if primaries else None

        if source_node and outbound_data[literal_field(source_node)] >= move.concurrent * BIG_SHARD_SIZE:
            continue

        zones = move.to_zone

        shards_for_this_index = wrap(jx.filter(all_shards, {
            "eq": {
                "index": shard.index
            }
        }))
        index_size = SUM(shards_for_this_index.size)
        existing_on_nodes = set(s.node.name for s in shards_for_this_index if s.status in {"INITIALIZING", "STARTED", "RELOCATING"} and s.i==shard.i)
        # FOR THE NODES WITH NO SHARDS, GIVE A DEFAULT VALUES
        node_weight = {
            n.name: coalesce(n.memory, 0)
            for n in nodes
        }
        for g, ss in jx.groupby(filter(lambda s: s.status == "STARTED" and s.node, shards_for_this_index), "node.name"):
            g = wrap_leaves(g)
            index_count = len(ss)
            node_weight[g.node.name] = nodes[g.node.name].memory * (1 - float(SUM(ss.size))/float(index_size+1))
            min_allowed = allocation[shard.index, g.node.name].min_allowed
            node_weight[g.node.name] *= 4 ** MIN([-1, min_allowed - index_count - 1])

        list_nodes = list(nodes)
        list_node_weight = [node_weight[n.name] for n in list_nodes]
        full_nodes = FlatList()
        good_reasons = 0
        for i, n in enumerate(list_nodes):
            alloc = allocation[shard.index, n.name]

            if n.zone.name not in zones:
                list_node_weight[i] = 0
            elif n.name in existing_on_nodes:
                list_node_weight[i] = 0
            elif inbound_data[literal_field(n.name)] >= move.concurrent * BIG_SHARD_SIZE:
                list_node_weight[i] = 0
                good_reasons += 1
            elif n.disk_free == 0 and n.disk > 0:
                list_node_weight[i] = 0
                full_nodes.append(n)
            elif n.disk and float(n.disk_free - shard.size) / float(n.disk) < 0.10 and move.reason != "not started":
                list_node_weight[i] = 0
                if move.reason != "slightly better balance":
                    full_nodes.append(n)  # WE ONLY CARE TO COMPLAIN IF IT IS NOT ABOUT FINE BALANCE
            elif n.disk and float(n.disk_free - shard.size) / float(n.disk) < 0.05:
                if move.reason == "not started":
                    Log.warning("Can not allocate shard {{shard}} to {{node}}", node=n.name, shard=(shard.index, shard.i))
                list_node_weight[i] = 0
                full_nodes.append(n)
            elif move.mode_priority >= 5 and len(alloc.shards) >= alloc.max_allowed:
                list_node_weight[i] = 0
                good_reasons += 1
            elif move.reason in {"not balanced", "slightly better balance"} and (
                        len(alloc.shards) >= alloc.min_allowed or  # IF THERE IS A MIS-BALANCE THEN THERE MUST BE A NODE WITH **LESS** THAN MINIMUM NUMBER OF SHARDS (PROBABLY FULL)
                        n.name in current_moving_shards.to_node    # SLOW DOWN MOVEMENT OF SHARDS, ENSURING THEY ARE PROPERLY ACCOUNTED FOR
            ):
                list_node_weight[i] = 0
                good_reasons += 1

        if SUM(list_node_weight) == 0:
            if not sent_full_nodes_warning and full_nodes and not good_reasons:
                sent_full_nodes_warning = True
                Log.warning(
                    "Can not move {{shard}} from {{source}} to {{destination}} because {{num}} nodes are all full",
                    shard=value2json({"index": shard.index, "i": shard.i}),
                    source=source_node,
                    destination=zones,
                    num=len(full_nodes),
                    full=[
                        {"name": n.name, "fill": mo_math.round(1 - (n.disk_free / n.disk), digits=2)}
                        for n in full_nodes
                    ]
                )
            continue  # NO SHARDS CAN ACCEPT THIS

        while True:
            i = Random.weight(list_node_weight)
            destination_node = list_nodes[i].name
            for s in all_shards:
                if s.index == shard.index and s.i == shard.i and s.node.name == destination_node:
                    Log.error(
                        "SHOULD NEVER HAPPEN Shard {{shard.index}}:{{shard.i}} already on node {{node}}",
                        shard=shard,
                        node=destination_node
                    )
                    break
            else:
                break

        existing = filter(
            lambda r: r.index == shard.index and r.i == shard.i and r.node.name == destination_node and r.status in {"INITIALIZING", "STARTED", "RELOCATING"},
            all_shards
        )
        if len(existing) >= nodes[destination_node].zone.shards:
            Log.error("should not happen")

        # DESTINATION HAS BEEN DECIDED, ISSUE MOVE

        if shard.status == "UNASSIGNED":
            if red_shards:
                command = wrap({ALLOCATE_EMPTY_PRIMARY: {
                    "accept_data_loss": ACCEPT_DATA_LOSS,
                    "index": shard.index,
                    "shard": shard.i,
                    "node": destination_node  # nodes[i].name,
                }})
                if ACCEPT_DATA_LOSS:
                    Log.warning(
                        "{{motivation}}: {{mode|upper}} index={{shard.index}}, shard={{shard.i}}, type={{shard.type}}, assign_to={{node}}",
                        motivation="Empty primary shard assigned!",
                        shard=shard,
                        node=destination_node
                    )
            else:
                command = wrap({ALLOCATE_REPLICA: {
                    "index": shard.index,
                    "shard": shard.i,
                    "node": destination_node  # nodes[i].name,
                }})
        elif shard.status == "STARTED":
            _move = {
                "index": shard.index,
                "shard": shard.i,
                "from_node": source_node,
                "to_node": destination_node
            }
            current_moving_shards.append(_move)
            command = wrap({"move": _move})
        else:
            Log.error("do not know how to handle")

        Log.note(
            "{{motivation}}: {{mode|upper}} index={{shard.index}}, shard={{shard.i}}, type={{shard.type}}, from={{from_node}}, assign_to={{node}}",
            mode=list(command.keys())[0],
            motivation=move.reason,
            shard=shard,
            from_node=source_node,
            node=destination_node
        )

        response = http.post(path + "/_cluster/reroute", json={"commands": [command]})
        result = json2value(response.content.decode('utf8'))

        def move_accepted():
            # CALL ME WHEN MOVE IS ACCEPTED
            if shard.status == "STARTED":
                shard.status = "RELOCATING"
            done.add((shard.index, shard.i))
            inbound_data[literal_field(destination_node)] += shard.size
            if source_node:
                # `source_node is None` WHEN CLUSTER IS RED
                outbound_data[literal_field(source_node)] += shard.size
            Log.note(
                "ok={{result.acknowledged}}",
                result=result
            )
            return 0

        if response.status_code in [200, 201] and result.acknowledged:
            move_failures = move_accepted()
            continue

        if move_failures >= MAX_MOVE_FAILURES:
            Log.warning("{{num}} consecutive failed moves. Starting over.", num=move_failures)
            return

        main_reason = strings.between(result.error, "[NO", "]")
        if main_reason and "target node version" in main_reason:
            continue

        move_failures += 1
        if main_reason and main_reason.find("too many shards on nodes for attribute") != -1:
            # THIS WILL HAPPEN WHEN THE ES SHARD BALANCER IS ACTIVATED, NOTHING WE CAN DO
            Log.note("Allocation failed: zone full. ES zone-based shard balancer activated")
        elif main_reason and main_reason.find("after allocation more than allowed") != -1:
            Log.note("Allocation failed: node out of space.")
        elif "failed to resolve [" in result.error:
            # LOST A NODE WHILE SENDING UPDATES
            lost_node_name = strings.between(result.error, "failed to resolve [", "]").strip()
            Log.warning("Allocation failed: Lost node during allocate {{node}}", node=lost_node_name)
            nodes[lost_node_name].zone = None
        elif main_reason and "there are too many copies of the shard" in main_reason:
            try:
                disable_zone_restrications(path)
                # TRY AGAIN
                Till(seconds=5).wait()
                response = http.post(path + "/_cluster/reroute", json={"commands": [command]})
                result = json2value(response.content.decode('utf8'))
                if response.status_code in [200, 201] and result.acknowledged:
                    move_failures = move_accepted()
                    continue
            except Exception as e:
                Log.warning("retry with disabled zone restrictions seems to have failed", cause=e)

        Log.warning(
            "Allocation failed: {{code}} Can not move/allocate:\n\treason={{reason}}\n\tdetails={{error|quote}}",
            code=response.status_code,
            reason=main_reason,
            error=result.error
        )

    Log.note("Done making moves")


def cancel(path, shard):
    json = {"commands": [{"cancel": {
        "index": shard.index,
        "shard": shard.i,
        "node": shard.node.name
    }}]}
    result = json2value(
        http.post(path + "/_cluster/reroute", json=json).content.decode('utf8')
    )
    if not result.acknowledged:
        main_reason = strings.between(result.error, "[NO", "]")
        Log.warning(
            "Can not cancel from {{node}}:\n\treason={{reason}}\n\tdetails={{error|quote}}",
            reason=main_reason,
            node=shard.node.name,
            error=result.error
        )
    else:
        Log.note(
            "index={{shard.index}}, shard={{shard.i}}, assign_to={{node}}, ok={{result.acknowledged}}",
            shard=shard,
            result=result,
            node=shard.node.name
        )

    Log.note("All moves made")


def balance_multiplier(shard_count, node_count):
    return 10 ** (mo_math.floor(float(shard_count) / float(node_count) + 0.9)-1)


def convert_table_to_list(table, column_names):
    lines = [l for l in table.split("\n") if l.strip()]

    # FIND THE COLUMNS WITH JUST SPACES
    columns = []
    for i, c in enumerate(zip(*lines)):
        if all(r == " " for r in c):
            columns.append(i)

    columns = columns[0:len(column_names)-1]
    for i, row in enumerate(lines):
        yield wrap({c: r for c, r in zip(column_names, split_at(row, columns))})


def split_at(row, columns):
    output = []
    last = 0
    for c in columns:
        output.append(row[last:c].strip())
        last = c
    output.append(row[last:].strip())
    return output


def text_to_bytes(size):
    if size == "":
        return 0

    multiplier = {
        "kb": 1000,
        "mb": 1000000,
        "gb": 1000000000
    }.get(size[-2:])
    if not multiplier:
        multiplier = 1
        if size[-1]=="b":
            size = size[:-1]
    else:
        size = size[:-2]
    try:
        return float(size) * float(multiplier)
    except Exception as e:
        Log.error("not expected", cause=e)


zone_restrictions_on = True  # KEEP THIS TRUE SO QUERIES GO TO spot, NOT backup NDOES


def disable_zone_restrications(path):
    global zone_restrictions_on
    if zone_restrictions_on:
        with Timer("Disable zone restrictions"):
            http.put(
                path + "/_cluster/settings",
                headers={"Content-Type": "application/json"},
                data=json.dumps({
                    "transient": {"cluster.routing.allocation.awareness.attributes": IDENTICAL_NODE_ATTRIBUTE}
                })
            )
    zone_restrictions_on = False


def enable_zone_restrictions(path):
    global zone_restrictions_on
    if not zone_restrictions_on:
        with Timer("Enable zone restrictions"):
            http.put(
                path + "/_cluster/settings",
                headers={"Content-Type": "application/json"},
                data=json.dumps({
                    "transient": {"cluster.routing.allocation.awareness.attributes": "zone"}
                })
            )
    zone_restrictions_on = True


def main():
    global zone_restrictions_on
    settings = startup.read_settings()
    Log.start(settings.debug)

    constants.set(settings.constants)
    path = settings.elasticsearch.host + ":" + text(settings.elasticsearch.port)

    try:
        # response = http.put(
        #     path + "/_cluster/settings",
        #     data='{"persistent": {"index.recovery.initial_shards": 1}}'
        # )
        # Log.note("ONE SHARD IS ENOUGH TO ALLOW WRITES: {{result}}", result=response.all_content)

        response = http.put(
            path + "/_cluster/settings",
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {
                    "persistent": {
                        "cluster.routing.allocation.enable": "none",
                        "cluster.routing.allocation.awareness.attributes": "zone",
                        "cluster.routing.allocation.awareness.force.zone.values": None,
                        "cluster.routing.allocation.balance.shard": 0.45,
                        "cluster.routing.allocation.balance.index": 0.55,
                        "cluster.routing.allocation.balance.threshold": 1,
                        "cluster.routing.use_adaptive_replica_selection": True
                    },
                    "transient": {
                        "cluster.routing.allocation.enable": "none",
                        "cluster.routing.allocation.awareness.attributes": None,
                        "cluster.routing.allocation.awareness.force.zone.values": None,
                        "cluster.routing.allocation.balance.shard": 0.0,
                        "cluster.routing.allocation.balance.index": 0.0,
                        "cluster.routing.allocation.balance.threshold": 1000,
                        "cluster.routing.use_adaptive_replica_selection": True
                    }
                }
            )

        )
        zone_restrictions_on = True
        Log.note("DISABLE SHARD MOVEMENT: {{result}}", result=response.all_content)

        response = http.put(
            path + "/_cluster/settings",
            headers={"Content-Type": "application/json"},
            data='{"transient": {"cluster.routing.allocation.disk.threshold_enabled" : false}}'
        )
        Log.note("ALLOW ALLOCATION: {{result}}", result=response.all_content)

        please_stop = Signal()

        def loop(please_stop):
            while not please_stop:
                try:
                    assign_shards(settings)
                except Exception as e:
                    Log.warning("Not expected", cause=e)
                (Till(seconds=30) | please_stop).wait()

        Thread.run("loop", loop, please_stop=please_stop)
        MAIN_THREAD.wait_for_shutdown_signal(please_stop=please_stop, allow_exit=True)
    except Exception as e:
        Log.error("Problem with assign of shards", e)
    finally:
        for p, command in settings["finally"].items():
            for c in listwrap(command):
                response = http.put(
                    path + p,
                    json=c
                )
                Log.note("Finally {{command}}\n{{result}}", command=c, result=response.all_content)

        Log.stop()


if __name__ == "__main__":
    main()
