"""Host-side Ensembl/VEP Perl API cache for docker builds (INSTALL.pl --AUTO a).

INSTALL.pl hits api.github.com for Ensembl API trees and often 403s. When
``.cache/vep-apis/<release>/ensembl-apis.tar.gz`` is ready, harness rewrites
the Dockerfile RUN to unpack Bio/ instead of calling INSTALL.pl.
"""

from __future__ import annotations

import os
import re

from .config import VEP_APIS_CACHE_DIR, VEP_APIS_RELEASE


VEP_APIS_TARBALL = "ensembl-apis.tar.gz"
VEP_APIS_CONTEXT = "tb2_vep_apis"


def vep_apis_release_dir(release: str | None = None) -> str:
    rel = (release or VEP_APIS_RELEASE).strip() or "115"
    return os.path.join(VEP_APIS_CACHE_DIR, rel)


def vep_apis_tarball_path(release: str | None = None) -> str:
    return os.path.join(vep_apis_release_dir(release), VEP_APIS_TARBALL)


def resolve_vep_apis_cache_ready(release: str | None = None) -> bool:
    override = os.getenv("TB2_USE_VEP_APIS_CACHE", "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    path = vep_apis_tarball_path(release)
    return os.path.isfile(path) and os.path.getsize(path) > 100_000


def vep_apis_cache_status_line() -> str:
    rel = VEP_APIS_RELEASE
    if resolve_vep_apis_cache_ready(rel):
        return (
            f"vep-apis 缓存: 已启用 (.cache/vep-apis/{rel}/{VEP_APIS_TARBALL})"
        )
    return (
        f"vep-apis 缓存: 未就绪（INSTALL.pl 仍会打 GitHub；先运行 "
        f".\\.cache\\cache_vep_apis.ps1 -Release {rel}）"
    )


def detect_vep_release_from_dockerfile(text: str) -> str | None:
    m = re.search(r"ensembl-vep-release-(\d+)", text)
    return m.group(1) if m else None


_INSTALL_PL_RE = re.compile(
    r"(?m)^RUN\s+cd\s+/app/data/ensembl-vep-release-(\d+)\s+&&\s*\\\s*\n"
    r"\s*perl\s+INSTALL\.pl\s+--AUTO\s+a\s+--NO_HTSLIB\s+--NO_TEST\s+--NO_UPDATE\s*$"
)


def rewrite_install_pl_to_cache(dockerfile_text: str, release: str) -> str | None:
    """Replace INSTALL.pl --AUTO a with COPY+unpack from BuildKit context.

    Returns rewritten text, or None if no matching RUN (nothing to do).
    """

    def _repl(m: re.Match[str]) -> str:
        rel = m.group(1)
        return (
            f"# tb2: Ensembl APIs from host cache (skip INSTALL.pl / GitHub)\n"
            f"COPY --from={VEP_APIS_CONTEXT} {VEP_APIS_TARBALL} "
            f"/tmp/{VEP_APIS_TARBALL}\n"
            f"RUN set -eux; \\\n"
            f"    cd /app/data/ensembl-vep-release-{rel}; \\\n"
            f"    tar -xzf /tmp/{VEP_APIS_TARBALL}; \\\n"
            f"    rm -f /tmp/{VEP_APIS_TARBALL}; \\\n"
            f"    test -d Bio/EnsEMBL; \\\n"
            f"    echo \"[tb2] vep-apis {rel} unpacked from cache\""
        )

    new, n = _INSTALL_PL_RE.subn(_repl, dockerfile_text)
    if n == 0:
        return None
    return new
