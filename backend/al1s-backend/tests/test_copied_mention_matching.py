import pytest

from al1s.meme_workshop.copied_mentions import CopiedMentionError, resolve_copied_targets


def member(user, nickname, card=""):
    return {"user_id": user, "nickname": nickname, "card": card}


def test_card_precedes_account_nickname_and_empty_card_uses_nickname():
    roster = [member(400, "Account", "Display"), member(500, "Nickname")]
    assert resolve_copied_targets("@Display", roster, "200") == ["400"]
    assert resolve_copied_targets("@Nickname", roster, "200") == ["500"]
    with pytest.raises(CopiedMentionError, match="not_found"):
        resolve_copied_targets("@Account", roster, "200")


def test_names_are_case_sensitive_and_normalize_width_and_outer_whitespace():
    roster = [member(400, "Alice Smith"), member(500, "\uff22\uff4f\uff42")]
    assert resolve_copied_targets("  @Alice Smith\u00a0 @Bob  ", roster, "200") == ["400", "500"]
    with pytest.raises(CopiedMentionError, match="not_found"):
        resolve_copied_targets("@alice smith", roster, "200")


def test_same_name_random_candidates_are_deduplicated_and_exclude_sender():
    roster = [member(200, "Same"), member(400, "Same"), member(400, "Same"), member(500, "Same")]
    offered = []

    def choose(values):
        offered.append(list(values))
        return values[-1]

    assert resolve_copied_targets("@Same", roster, "200", choose=choose) == ["500"]
    assert offered == [["400", "500"]]


def test_self_only_never_falls_back_to_self():
    with pytest.raises(CopiedMentionError, match="self_only"):
        resolve_copied_targets("@Me", [member(200, "Me")], "200")


def test_multiple_targets_preserve_order_and_at_inside_a_name():
    roster = [member(400, "a@b"), member(500, "Person Two")]
    assert resolve_copied_targets("@Person Two @a@b @Person Two", roster, "200") == [
        "500",
        "400",
        "500",
    ]


def test_ambiguous_name_partition_is_not_randomly_reinterpreted():
    roster = [member(400, "A"), member(500, "B"), member(600, "A @B")]
    with pytest.raises(CopiedMentionError, match="ambiguous"):
        resolve_copied_targets("@A @B", roster, "200")


def test_conflicting_names_for_one_member_reject_the_roster():
    roster = [member(400, name) for name in ("A", "A @B", "B @C", "C")]
    with pytest.raises(CopiedMentionError, match="members_unavailable"):
        resolve_copied_targets("@A @B @C", roster, "200")


@pytest.mark.parametrize(
    "roster",
    [
        [member(True, "Bad")],
        [member(400, "Name", False)],
        [{"user_id": "u_unknown", "nickname": "Name"}],
        [member(400, "Name")] * 10_001,
    ],
)
def test_incomplete_or_oversized_rosters_are_rejected(roster):
    with pytest.raises(CopiedMentionError, match="unavailable"):
        resolve_copied_targets("@Name", roster, "200")


def test_input_and_target_count_are_bounded():
    roster = [member(400, "Name")]
    for text in ("@" + "x" * 200, "@Name @Name @Name @Name @Name"):
        with pytest.raises(CopiedMentionError):
            resolve_copied_targets(text, roster, "200")
