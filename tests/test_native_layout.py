import copy
import hashlib
import json
import unittest

import native_layout


def identity(user="fixture", account="workspace"):
    atom = {"userId": user, "accountId": account}
    owner = hashlib.sha256(json.dumps([user, account], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return owner, {native_layout.IDENTITY: atom}


def shared():
    owner, atoms = identity()
    return {
        native_layout.CONTENT_OWNER: owner,
        native_layout.OWNER_ATOMS + owner: atoms,
        native_layout.OWNER_ATOMS + native_layout.SIGNED_OUT: {},
        native_layout.SHARED_ATOMS: {"prompt-history": {"private-scope": ["keep verbatim"]}},
        "local-projects": {"local": {"name": "untouched"}},
    }


class NativeLayout(unittest.TestCase):
    def test_shared_keeps_exact_data_and_exposes_identity_listing(self):
        state = shared()
        for i in range(4):
            owner, atom = identity("other-" + str(i))
            state[native_layout.OWNER_ATOMS + owner] = atom
        original = copy.deepcopy(state)
        result = native_layout.inspect(state)
        self.assertEqual(result["kind"], "shared-v2")
        self.assertEqual(len(result["owners"]), 5)
        self.assertEqual(result["active"], state[native_layout.CONTENT_OWNER])
        self.assertIs(result["owners"][result["active"]], state[native_layout.OWNER_ATOMS + result["active"]])
        rows = native_layout.profiles(state)
        self.assertEqual(sum(r["active"] for r in rows), 1)
        self.assertTrue(all(set(r) == {"label", "active"} and len(r["label"]) == 12 for r in rows))
        self.assertEqual(state, original)

    def test_shared_signed_out_or_new_empty_requires_positive_layout_marker(self):
        for active in (None, native_layout.SIGNED_OUT):
            state = {native_layout.CONTENT_OWNER: active, native_layout.SHARED_ATOMS: {}}
            self.assertEqual(native_layout.inspect(state),
                             {"kind": "shared-v2", "active": None, "owners": {}})
        for state in ({}, {native_layout.SHARED_ATOMS: {}},
                      {native_layout.OWNER_ATOMS + native_layout.SIGNED_OUT: {}}):
            with self.assertRaisesRegex(ValueError, "^unsupported-identity-layout$"):
                native_layout.inspect(state)

    def test_shared_prompt_cache_is_never_read_from_owner_atoms(self):
        class Poison(dict):
            def __iter__(self):
                raise AssertionError("prompt body inspected")
            def items(self):
                raise AssertionError("prompt body inspected")
        state = shared()
        state[native_layout.SHARED_ATOMS]["prompt-history"] = Poison()
        self.assertEqual(native_layout.inspect(state)["kind"], "shared-v2")
        key = native_layout.OWNER_ATOMS + state[native_layout.CONTENT_OWNER]
        state[key]["prompt-history"] = Poison()
        with self.assertRaisesRegex(ValueError, "^unexpected-owner-prompt-history$"):
            native_layout.inspect(state)

    def test_shared_bad_identity_missing_owner_and_malformed_atom_refused(self):
        for alteration, code in (
            (lambda s: s[native_layout.OWNER_ATOMS + s[native_layout.CONTENT_OWNER]][native_layout.IDENTITY].update(userId="different"),
             "identity-proof-mismatch"),
            (lambda s: s.pop(native_layout.OWNER_ATOMS + s[native_layout.CONTENT_OWNER]),
             "active-native-owner-missing"),
            (lambda s: s.update({native_layout.OWNER_ATOMS + "unknown": {}}),
             "unsupported-profile-layout"),
            (lambda s: s.update({native_layout.OWNER_ATOMS + s[native_layout.CONTENT_OWNER]: []}),
             "unsupported-profile-layout"),
            (lambda s: s[native_layout.OWNER_ATOMS + s[native_layout.CONTENT_OWNER]].clear(),
             "uninitialized-native-owner"),
            (lambda s: s[native_layout.OWNER_ATOMS + native_layout.SIGNED_OUT].update({native_layout.IDENTITY: {"userId": "x"}}),
             "signed-out-identity-conflict"),
        ):
            state = shared()
            alteration(state)
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "^" + code + "$"):
                native_layout.inspect(state)

    def test_mixed_layout_rejected_and_legacy_uninitialized_skipped(self):
        owner, atoms = identity()
        idle, _ = identity("idle")
        state = {native_layout.LEGACY: {"owner": owner, "inactive": {
            idle: {"globals": {}, "atoms": {}}}}, native_layout.SHARED_ATOMS: atoms}
        result = native_layout.inspect(state)
        self.assertEqual(result, {"kind": "legacy", "active": owner, "owners": {owner: atoms}})
        state[native_layout.CONTENT_OWNER] = owner
        with self.assertRaisesRegex(ValueError, "^mixed-native-layout$"):
            native_layout.inspect(state)
        del state[native_layout.CONTENT_OWNER]
        state[native_layout.LEGACY]["owner"] = None
        self.assertEqual(native_layout.inspect(state), {"kind": "legacy", "active": None, "owners": {}})

    def test_legacy_incomplete_corrupt_and_duplicate_refused(self):
        owner, atoms = identity()
        cases = [
            {native_layout.LEGACY: {}},
            {native_layout.LEGACY: {"owner": owner, "inactive": {}}},
            {native_layout.LEGACY: {"owner": None, "inactive": {owner: {"atoms": atoms}}}},
            {native_layout.LEGACY: {"owner": owner, "inactive": {
                owner: {"globals": {}, "atoms": atoms}}}, native_layout.SHARED_ATOMS: atoms},
        ]
        for state in cases:
            with self.assertRaises(ValueError) as error:
                native_layout.inspect(state)
            self.assertNotIn(owner, str(error.exception))


if __name__ == "__main__":
    unittest.main()
