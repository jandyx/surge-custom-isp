# Surge ISP panel bundle

Vendored local copies used by `scripts/surge-isp.py setup` before the upstream
Rules repository is published or refreshed.

Provenance:
- `Panel.sgmodule` is based on the original Rules repository’s `Surge/Module/Panel.sgmodule` (`#!name=Unlock & IP Risk Panel`) with one managed `isp_check` script and panel row.
- `isp_check.js` is the managed ISP exit panel script. It queries `https://my.ippure.com/v1/info` through `policy=Custom-ISP` for the exit IPv4, country, IP type and risk score, with a same-policy IPv4-only fallback. An Anthropic request checks connectivity; an optional `expected_ip` mismatch is shown only as a warning. Classification is third-party metadata, not proof of residential service or account access.

The installer deploys `isp_check.js` to the user-private Surge Profiles directory,
renders the module with that absolute local script path, and enables the module
through `surge-cli module enable`.

The historical Rules checkout is not a runtime dependency. The bundled module also
contains optional third-party panel scripts loaded from public CDNs; these are
not required by the local ISP CLI. Preserve upstream attribution when sharing.
