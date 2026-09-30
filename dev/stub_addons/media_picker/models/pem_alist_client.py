# -*- coding: utf-8 -*-
"""Stub of media_picker's read-only Alist client (tests mock this)."""
import requests


def get_file(source, path):
    resp = requests.post(
        f"{source.alist_url.rstrip('/')}/api/fs/get",
        json={'path': path},
        headers={'Authorization': source.alist_token or ''},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get('data') or {}
