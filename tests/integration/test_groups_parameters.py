"""Groups + parameters API integration tests."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.conftest import auth_h


async def _mk_group(
    client: AsyncClient, token: str, name: str, parent_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = await client.post("/api/v1/groups", headers=auth_h(token), json=body)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


async def _mk_host(client: AsyncClient, token: str, name: str) -> str:
    r = await client.post(
        "/api/v1/hosts", headers=auth_h(token), json={"hostname": name, "status": "active"}
    )
    assert r.status_code == 201
    host_id: str = r.json()["id"]
    return host_id


async def _global_id(client: AsyncClient, token: str) -> str:
    r = await client.get("/api/v1/groups", headers=auth_h(token))
    assert r.status_code == 200
    gid: str = next(g["id"] for g in r.json() if g["name"] == "global")
    return gid


async def test_global_group_exists_and_is_protected(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)

    r = await client.patch(
        f"/api/v1/groups/{gid}", headers=auth_h(admin_token), json={"name": "renamed"}
    )
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/groups/{gid}", headers=auth_h(admin_token))
    assert r.status_code == 409
    # No duplicate root.
    r = await client.post("/api/v1/groups", headers=auth_h(admin_token), json={"name": "global"})
    assert r.status_code == 409


async def test_group_tree_crud_and_cycle_guard(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)
    east = await _mk_group(client, admin_token, "dc-east")
    assert east["parent_id"] == gid  # parent defaults to global
    web = await _mk_group(client, admin_token, "web", parent_id=east["id"])

    # Reparenting under a descendant is refused.
    r = await client.patch(
        f"/api/v1/groups/{east['id']}",
        headers=auth_h(admin_token),
        json={"parent_id": web["id"]},
    )
    assert r.status_code == 409

    # Deleting a group with children is refused; leaf delete works.
    r = await client.delete(f"/api/v1/groups/{east['id']}", headers=auth_h(admin_token))
    assert r.status_code == 409
    r = await client.delete(f"/api/v1/groups/{web['id']}", headers=auth_h(admin_token))
    assert r.status_code == 204
    r = await client.delete(f"/api/v1/groups/{east['id']}", headers=auth_h(admin_token))
    assert r.status_code == 204


async def test_group_mutations_are_admin_only(
    client: AsyncClient, admin_token: str, user_token: str
) -> None:
    r = await client.post("/api/v1/groups", headers=auth_h(user_token), json={"name": "nope"})
    assert r.status_code == 403
    # Reads are fine for any user.
    r = await client.get("/api/v1/groups", headers=auth_h(user_token))
    assert r.status_code == 200


async def test_membership_and_effective_parameters(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)
    east = await _mk_group(client, admin_token, "dc-east")
    web = await _mk_group(client, admin_token, "web", parent_id=east["id"])
    host_id = await _mk_host(client, admin_token, "gp-node")

    # global membership is implicit and cannot be made explicit.
    r = await client.post(
        f"/api/v1/groups/{gid}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    assert r.status_code == 409

    r = await client.post(
        f"/api/v1/groups/{web['id']}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    assert r.status_code == 204
    r = await client.get(f"/api/v1/groups/{web['id']}/members", headers=auth_h(admin_token))
    assert r.json() == [host_id]
    r = await client.get(f"/api/v1/hosts/{host_id}/groups", headers=auth_h(admin_token))
    assert [g["name"] for g in r.json()] == ["web"]

    # Parameters at every level of the chain + host override.
    for target, value in [
        (f"/api/v1/groups/{gid}/parameters/ntp", "pool.ntp.org"),
        (f"/api/v1/groups/{east['id']}/parameters/ntp", "ntp.east.internal"),
        (f"/api/v1/groups/{east['id']}/parameters/syslog", "syslog.east"),
        (f"/api/v1/groups/{web['id']}/parameters/role", "web"),
    ]:
        r = await client.put(target, headers=auth_h(admin_token), json={"value": value})
        assert r.status_code == 200, r.text

    r = await client.put(
        f"/api/v1/hosts/{host_id}/parameters/role",
        headers=auth_h(admin_token),
        json={"value": "canary"},
    )
    assert r.status_code == 200

    r = await client.get(
        f"/api/v1/hosts/{host_id}/effective-parameters", headers=auth_h(admin_token)
    )
    assert r.status_code == 200
    effective = {p["key"]: p for p in r.json()}
    assert effective["ntp"]["value"] == "ntp.east.internal"
    assert effective["ntp"]["source_name"] == "dc-east"
    assert effective["syslog"]["value"] == "syslog.east"
    assert effective["role"]["value"] == "canary"
    assert effective["role"]["source_kind"] == "host"

    # Unset the host override: the group value shows through again.
    r = await client.delete(f"/api/v1/hosts/{host_id}/parameters/role", headers=auth_h(admin_token))
    assert r.status_code == 204
    r = await client.get(
        f"/api/v1/hosts/{host_id}/effective-parameters", headers=auth_h(admin_token)
    )
    effective = {p["key"]: p for p in r.json()}
    assert effective["role"]["value"] == "web"
    assert effective["role"]["source_name"] == "web"


async def test_invalid_parameter_key_rejected(client: AsyncClient, admin_token: str) -> None:
    gid = await _global_id(client, admin_token)
    r = await client.put(
        f"/api/v1/groups/{gid}/parameters/Bad-Key",
        headers=auth_h(admin_token),
        json={"value": 1},
    )
    assert r.status_code == 422


async def test_group_audit_trail(client: AsyncClient, admin_token: str) -> None:
    east = await _mk_group(client, admin_token, "audit-grp")
    host_id = await _mk_host(client, admin_token, "audit-node")
    await client.post(
        f"/api/v1/groups/{east['id']}/members",
        headers=auth_h(admin_token),
        json={"host_id": host_id},
    )
    await client.put(
        f"/api/v1/groups/{east['id']}/parameters/tz",
        headers=auth_h(admin_token),
        json={"value": "UTC"},
    )
    audit = await client.get("/api/v1/audit", headers=auth_h(admin_token))
    actions = {e["action"] for e in audit.json()["items"]}
    assert {"group.create", "group.add_member", "group.set_parameter"} <= actions


# --- Effective membership (rolls up the tree) -------------------------------
#
# The tree these use is the one that reported the bug:
#
#     global
#     └── kube
#         ├── kubemasters   (1 host)
#         └── kubeworkers   (2 hosts)


def _members_cell(html: str, group_name: str) -> str:
    """The Members count the groups page renders for one group.

    Parsed out of the row rather than searched for anywhere on the page, so a
    3 belonging to some other group cannot make this pass.
    """
    import re

    row = re.search(
        rf">{group_name}</a>.*?</tr>",
        html,
        re.S,
    )
    assert row is not None, f"no row for {group_name}"
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(0), re.S)
    # Row is: name (already consumed above), description, members, parameters.
    return re.sub(r"<[^>]+>", "", cells[1]).strip()


async def _kube_tree(client: AsyncClient, token: str) -> dict[str, Any]:
    kube = await _mk_group(client, token, "kube")
    masters = await _mk_group(client, token, "kubemasters", parent_id=kube["id"])
    workers = await _mk_group(client, token, "kubeworkers", parent_id=kube["id"])
    hosts = {
        "m1": await _mk_host(client, token, "m1"),
        "w1": await _mk_host(client, token, "w1"),
        "w2": await _mk_host(client, token, "w2"),
    }
    for group, host in ((masters, "m1"), (workers, "w1"), (workers, "w2")):
        r = await client.post(
            f"/api/v1/groups/{group['id']}/members",
            headers=auth_h(token),
            json={"host_id": hosts[host]},
        )
        assert r.status_code == 204, r.text
    return {"kube": kube, "masters": masters, "workers": workers, "hosts": hosts}


async def test_parent_group_counts_its_descendants_hosts(
    client: AsyncClient, admin_token: str, admin_user: Any
) -> None:
    """The reported bug: `kube` read 0 while its two subgroups held 3 hosts."""
    from tests.integration.test_ui import _ui_login

    await _kube_tree(client, admin_token)
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get("/groups")
    assert r.status_code == 200
    assert _members_cell(r.text, "kube") == "3"
    # And the subgroups still report only their own.
    assert _members_cell(r.text, "kubemasters") == "1"
    assert _members_cell(r.text, "kubeworkers") == "2"


async def test_a_host_in_two_subgroups_counts_once(client: AsyncClient, admin_token: str) -> None:
    tree = await _kube_tree(client, admin_token)
    r = await client.post(
        f"/api/v1/groups/{tree['masters']['id']}/members",
        headers=auth_h(admin_token),
        json={"host_id": tree["hosts"]["w1"]},
    )
    assert r.status_code == 204

    r = await client.get(
        f"/api/v1/groups/{tree['kube']['id']}/members?include_descendants=true",
        headers=auth_h(admin_token),
    )
    assert sorted(r.json()) == sorted(tree["hosts"].values())


async def test_members_endpoint_stays_direct_by_default(
    client: AsyncClient, admin_token: str
) -> None:
    """Existing callers keep the meaning they were written against; the rolled
    up set is opt-in."""
    tree = await _kube_tree(client, admin_token)

    r = await client.get(
        f"/api/v1/groups/{tree['kube']['id']}/members", headers=auth_h(admin_token)
    )
    assert r.json() == []

    r = await client.get(
        f"/api/v1/groups/{tree['kube']['id']}/members?include_descendants=true",
        headers=auth_h(admin_token),
    )
    assert sorted(r.json()) == sorted(tree["hosts"].values())


async def test_the_root_covers_hosts_that_are_in_no_group(
    client: AsyncClient, admin_token: str
) -> None:
    """Membership in `global` is implicit and never written to `host_groups`,
    so a plain subtree query would miss an ungrouped host entirely."""
    tree = await _kube_tree(client, admin_token)
    loner = await _mk_host(client, admin_token, "ungrouped")
    gid = await _global_id(client, admin_token)

    r = await client.get(
        f"/api/v1/groups/{gid}/members?include_descendants=true", headers=auth_h(admin_token)
    )
    assert sorted(r.json()) == sorted([*tree["hosts"].values(), loner])


async def test_the_group_page_names_why_an_inherited_host_is_listed(
    client: AsyncClient, admin_token: str, admin_user: Any
) -> None:
    from tests.integration.test_ui import _ui_login

    tree = await _kube_tree(client, admin_token)
    await _ui_login(client, "admin@example.com", "admin-pw-1234")

    r = await client.get(f"/groups/{tree['kube']['id']}")
    assert r.status_code == 200
    for hostname in ("m1", "w1", "w2"):
        assert f">{hostname}</a>" in r.text
    assert "kubeworkers" in r.text
    assert "kubemasters" in r.text
    # Nothing is a direct member of `kube`, so no row may offer Remove: it
    # would be a no-op dressed up as an action.
    assert "Remove" not in r.text

    r = await client.get(f"/groups/{tree['workers']['id']}")
    assert "Remove" in r.text  # direct members, where removing means something


async def test_direct_membership_is_unchanged(client: AsyncClient, admin_token: str) -> None:
    """The rollup is a read. Adding still writes one row, to the group named."""
    tree = await _kube_tree(client, admin_token)

    r = await client.get(
        f"/api/v1/groups/{tree['workers']['id']}/members", headers=auth_h(admin_token)
    )
    assert sorted(r.json()) == sorted([tree["hosts"]["w1"], tree["hosts"]["w2"]])

    r = await client.get(f"/api/v1/hosts/{tree['hosts']['w1']}/groups", headers=auth_h(admin_token))
    assert [g["name"] for g in r.json()] == ["kubeworkers"]

    r = await client.delete(
        f"/api/v1/groups/{tree['workers']['id']}/members/{tree['hosts']['w1']}",
        headers=auth_h(admin_token),
    )
    assert r.status_code == 204
    r = await client.get(
        f"/api/v1/groups/{tree['kube']['id']}/members?include_descendants=true",
        headers=auth_h(admin_token),
    )
    assert sorted(r.json()) == sorted([tree["hosts"]["m1"], tree["hosts"]["w2"]])
