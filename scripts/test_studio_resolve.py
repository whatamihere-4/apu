"""Quick checks for studio/network folder resolution."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as a

os.environ.setdefault("HASHES_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache"))


def check(sub, net, expect_display, expect_folder_substr):
    out, url, meta = a._resolve_studio_for_autofill(sub, net)
    ok = out == expect_display and (expect_folder_substr in (url or ""))
    status = "OK" if ok else "FAIL"
    print(f"{status}  {sub!r} + {net!r}")
    print(f"      display={out!r}  url={url}  meta={meta}")
    if not ok:
        print(f"      expected display={expect_display!r} url contains {expect_folder_substr!r}")
    return ok


def main():
    tests = [
        ("FANS", "Naughty America (Network)", "FANS / Naughty America", "7b72ea46"),
        ("Virtual Papi", "SexLikeReal (Network)", "Virtual Papi / SexLikeReal", "4807ec8e"),
        ("WankzVR", None, "WankzVR", "2c4e4f14"),
        ("POVR", None, "POVR", "538ef92f"),
    ]
    vr = a._strip_vr_from_label("Wankz VR")
    assert vr == "Wankz", f"VR strip got {vr!r}"
    assert a._strip_vr_from_label("POVR") == "POVR"
    assert a._strip_network_suffix("SexLikeReal (Network)") == "SexLikeReal"

    failed = sum(0 if check(*t) else 1 for t in tests)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
