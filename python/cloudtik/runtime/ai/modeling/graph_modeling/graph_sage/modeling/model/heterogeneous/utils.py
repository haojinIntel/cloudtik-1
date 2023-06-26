"""
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Author: Chen Haifeng
"""

import torch


# Most functions operating for heterogeneous graph are on dict of tensors

def get_node_types(g, relations):
    node_types = set()
    for etype in g.canonical_etypes:
        if etype[1] in relations:
            node_types.add(etype[0])
            node_types.add(etype[2])
    return node_types


def get_node_indices(g, relations):
    node_types = get_node_types(g, relations)
    return {node_type: torch.arange(
        g.num_nodes(node_type)).to(g.device) for node_type in node_types}


def get_num_nodes_of_graph(g, relations):
    node_types = get_node_types(g, relations)
    return {node_type: g.num_nodes(
        node_type) for node_type in node_types}


def get_num_nodes_of_nids(g, relations, nids):
    node_types = get_node_types(g, relations)
    return {node_type: ids.size(
        dim=0) for node_type, ids in nids.items() if node_type in node_types}


def get_total_num_nodes(nids):
    num_nodes = 0
    for ntype, ids in nids.items():
        num_nodes += ids.size(dim=0)
    return num_nodes


def tensor_dict_new(
        num_nodes, out_size,
        output_device, pin_memory):
    y = {}
    for ntype, num in num_nodes.items():
        y[ntype] = torch.empty(
            num,
            out_size,
            device=output_device,
            pin_memory=pin_memory,
        )
    return y


def tensor_dict_to(x, device):
    return {k: v.to(device) for k, v in x.items()}


def tensor_dict_store(x, y, indices, device):
    for k, v in x.items():
        i = indices[k]
        y_v = y[k]
        y_v[i] = v.to(device)


def tensor_dict_store_at(x, y, indices, pos, device):
    for k, v in x.items():
        start = pos[k]
        end = start + indices[k].size(dim=0)
        y_v = y[k]
        y_v[start: end] = v.to(device)
        pos[k] = end


def tensor_dict_collect(x, indices):
    return {k: x[k][i] for k, i in indices.items()}


def tensor_dict_flatten(x):
    # we should the sort key, so that it is predictable
    return torch.cat([v for k, v in sorted(x.items())])


def tensor_dict_shape(x):
    return {k: v.shape for k, v in x.items()}


def parse_reverse_edges(reverse_edges_str):
    reverse_edge_dict = {}
    reverse_edges = [x.strip() for x in reverse_edges_str.split(",")]
    for reverse_edge in reverse_edges:
        reverse_edge_parts = [x.strip() for x in reverse_edge.split(":")]
        if len(reverse_edge_parts) != 2:
            raise ValueError(
                "Invalid reverse edge specification. Format: edge_type:reverse_edge_type")
        reverse_edge_dict[reverse_edge_parts[0]] = reverse_edge_parts[1]
    return reverse_edge_dict


def get_edix_from_mask(g, mask_name):
    edix_dict = {}
    for etype in g.canonical_etypes:
        edge_name = etype[1]
        mask = g.edges[edge_name].data[mask_name]
        edix_dict[edge_name] = torch.nonzero(mask, as_tuple=False).squeeze()
    return edix_dict