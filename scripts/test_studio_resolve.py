"""Quick checks for studio/network folder resolution."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as a

os.environ.setdefault("HASHES_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache"))


def check(sub, net, expect_display, expect_folder_substr, expect_link_label=None):
    out, url, meta = a._resolve_studio_for_autofill(sub, net)
    ok = out == expect_display and (expect_folder_substr in (url or ""))
    if expect_link_label is not None:
        ok = ok and meta.get("studio_link_label") == expect_link_label
    status = "OK" if ok else "FAIL"
    print(f"{status}  {sub!r} + {net!r}")
    print(f"      display={out!r}  url={url}  meta={meta}")
    if not ok:
        print(f"      expected display={expect_display!r} url contains {expect_folder_substr!r}")
        if expect_link_label is not None:
            print(f"      expected studio_link_label={expect_link_label!r}")
    return ok


def main():
    tests = [
        ("FANS", "Naughty America (Network)", "FANS", "7b72ea46", "Naughty America"),
        ("Virtual Papi", "SexLikeReal (Network)", "Virtual Papi", "4807ec8e", "SexLikeReal"),
        ("WankzVR", None, "WankzVR", "2c4e4f14", None),
        ("POVR", None, "POVR", "538ef92f", None),
    ]
    vr = a._strip_vr_from_label("Wankz VR")
    assert vr == "Wankz", f"VR strip got {vr!r}"
    assert a._strip_vr_from_label("POVR") == "POVR"
    assert a._strip_network_suffix("SexLikeReal (Network)") == "SexLikeReal"
    assert a._parse_studio_display_label("The Dressing Room / Naughty America") == (
        "The Dressing Room",
        "Naughty America",
    )

    combined_out, combined_url, combined_meta = a._resolve_studio_for_autofill(
        "The Dressing Room / Naughty America"
    )
    assert combined_out == "The Dressing Room", combined_out
    assert combined_meta.get("studio_link_label") == "Naughty America", combined_meta
    assert combined_url and "7b72ea46" in combined_url, combined_url

    failed = sum(0 if check(*t) else 1 for t in tests)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
