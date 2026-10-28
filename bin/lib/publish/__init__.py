"""Public-mirror exporter: build, scan, diff and release a cleaned copy of a private repo.

The public repo only ever receives cleaned exports with no shared history. The pipeline fails
closed at every step: a manifest is default-deny, the source must be clean and pushed, the scan
must pass on the identical tree, and the release clone's pre-push hook scans again.

    common    where things live (private data on the private host, work dirs, sources)
    manifest  the per-repo TOML manifest (include/exclude, replacements, allowances, remote)
    build     the source repo's HEAD -> a manifest-filtered tree
    scan      every scanner class over a tree; JSON report to the private store
    release   diff against the last release, the squashed commit, the pre-push hook, verify

Sensitive data (names, terms, hosts, manifests, reports, the release log) lives only under
`<private store>/publish/`; nothing here names an account, host or person.
"""
