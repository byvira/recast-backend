from app.pipelines.text.claims import unsupported_claims

SOURCE = "Recast costs Rs 4,999 a month. It writes in 30 languages. I managed content for 11 brands at once."


def test_numbers_that_are_in_the_source_pass():
    text = "Recast is Rs 4,999 a month, covers 30 languages, and I handled 11 brands."
    assert unsupported_claims(text, [SOURCE]) == []


def test_invented_figures_prices_scores_and_events_are_listed():
    text = ("Last Thursday I ran a side-by-side test. Recast scored 96% on keywords, 8.6 on readability, "
            "and the freelancer billed $22 for 12 minutes. A client told me it was 3x faster.")
    out = unsupported_claims(text, [SOURCE])
    joined = " ".join(out)
    for needle in ("96%", "8.6", "$22", "12 minutes", "3x", "Last Thursday", "I ran a side-by-side test", "A client told me"):
        assert needle.lower() in joined.lower(), (needle, out)


def test_a_paraphrased_real_story_is_not_flagged():
    source = "I tested the tone checker against our old process for a week and the old process drifted."
    assert unsupported_claims("I tested the tone checker for a week, and our old process drifted.", [source]) == []


def test_nothing_to_compare_with_means_nothing_is_flagged():
    assert unsupported_claims("It is 99% faster.", []) == []
    assert unsupported_claims("", [SOURCE]) == []


def test_indic_digits_and_commas_compare_as_the_same_number():
    assert unsupported_claims("மாதம் ரூ ₹ ४,९९९", ["Price: 4999 per month"]) == []


def test_the_list_is_capped():
    text = " ".join(f"{n}% " for n in range(11, 40))
    assert len(unsupported_claims(text, ["nothing here"])) == 8
