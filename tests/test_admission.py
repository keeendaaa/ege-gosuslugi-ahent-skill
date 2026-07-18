import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "admission.py"
SPEC = importlib.util.spec_from_file_location("admission", SCRIPT)
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)


def test_parse_arbitrary_scores_and_aliases():
    scores = admission.parse_scores(
        ["Русский=55", "Профильная математика=72", "Инфа=70", "4=74"]
    )

    assert scores == {
        "русский язык": 55,
        "математика": 72,
        "информатика": 70,
        "id:4": 74,
    }


def test_parse_scores_validates_range():
    with pytest.raises(ValueError, match="between 0 and 100"):
        admission.parse_scores(["Физика=101"])


def make_test(test_id, subject_id, name, priority, minimum, replacement=None):
    return {
        "id": test_id,
        "subject": {"id": subject_id, "name": name},
        "priority": priority,
        "minScore": minimum,
        "replaceEntranceTestId": replacement,
    }


TESTS = [
    make_test(1, 2, "Математика", 1, 40),
    make_test(2, 3, "Информатика", 2, 46),
    make_test(3, 4, "Физика", 2, 41, replacement=2),
    make_test(4, 1, "Русский язык", 3, 40),
]


def test_alternative_exam_uses_best_supplied_score():
    scores = admission.parse_scores(
        ["Русский язык=55", "Математика=72", "Информатика=70", "Физика=74"]
    )

    assert admission.candidate_results(TESTS, scores) == (72, 74, 55)


def test_auto_levels_include_basic_higher_but_not_specialist():
    catalog = [
        {
            "educationLevelId": level,
            "educationFormId": 1,
            "placeTypeId": 1,
            "stageAdmissionId": 1,
            "numberPlaces": 10,
        }
        for level in (2, 3, 6)
    ]

    assert admission.resolve_levels(catalog, None, False, 1) == [2, 6]
    assert admission.resolve_levels(catalog, None, True, 1) == [2, 6, 3]
    assert admission.resolve_levels(catalog, [3], False, 1) == [3]


def test_select_organization_excludes_branch():
    result = admission.select_organization(
        [
            {
                "organizationId": 27,
                "name": "РУТ",
                "city": "Москва",
                "filial": False,
                "parentOrgId": 0,
            },
            {
                "organizationId": 999,
                "name": "Филиал РУТ",
                "city": "Москва",
                "filial": True,
                "parentOrgId": 27,
            },
        ],
        "РУТ",
    )

    assert result["organizationId"] == 27


def test_ranking_handles_bvi_exclusions_and_exact_ties():
    class Client:
        organization_id = 27

        def applicants(self, competition_id):
            return {
                "updateDate": "2026-07-19T00:00:00+03:00",
                "applicants": [
                    {"consent": "ONLINE", "withoutTests": True, "sumMark": 0},
                    {"consent": "OFFLINE", "withoutTests": False, "sumMark": 203},
                    {
                        "consent": "ONLINE",
                        "withoutTests": False,
                        "sumMark": 202,
                        "result1": 80,
                        "result2": 70,
                        "result3": 55,
                    },
                    {
                        "consent": "ONLINE",
                        "withoutTests": False,
                        "sumMark": 202,
                        "result1": 72,
                        "result2": 70,
                        "result3": 55,
                    },
                    {"consent": "NONE", "withoutTests": False, "sumMark": 300},
                    {
                        "consent": "ONLINE",
                        "withoutTests": False,
                        "sumMark": 300,
                        "statusName": "Конкурсная группа исключена",
                    },
                ],
            }

    group = {
        "id": 123,
        "oksoCode": "2.09.03.01",
        "oksoName": "Информатика и вычислительная техника",
        "educationLevelId": 2,
        "educationLevelName": "Бакалавриат",
        "educationFormId": 1,
        "educationFormName": "Очная",
        "numberPlaces": 4,
        "entranceTests": TESTS,
        "programs": [{"id": 99, "name": "Программная инженерия"}],
    }
    scores = admission.parse_scores(
        ["Русский язык=55", "Математика=72", "Информатика=70"]
    )

    result = admission.analyze_group(Client(), group, scores, 5, None)

    assert result["totalScore"] == 202
    assert result["activeConsents"] == 4
    assert (result["placeBest"], result["placeWorst"]) == (4, 5)
    assert result["verdict"] == "borderline"
