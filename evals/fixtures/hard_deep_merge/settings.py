"""Layered settings: defaults <- project <- user."""

from merge import merge


def resolve(defaults, project, user):
    return merge(merge(defaults, project), user)
