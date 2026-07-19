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


def test_available_levels_filters_by_place_type_and_cost():
    catalog = [
        {"educationLevelId": 2, "educationFormId": 1, "placeTypeId": 1, "stageAdmissionId": 1, "numberPlaces": 10},
        {"educationLevelId": 2, "educationFormId": 1, "placeTypeId": 3, "stageAdmissionId": 1, "numberPlaces": 5, "costOfStudy": 300_000},
        {"educationLevelId": 2, "educationFormId": 1, "placeTypeId": 3, "stageAdmissionId": 1, "numberPlaces": 5, "costOfStudy": 600_000},
        {"educationLevelId": 6, "educationFormId": 1, "placeTypeId": 3, "stageAdmissionId": 1, "numberPlaces": 5, "costOfStudy": 250_000},
    ]

    assert admission.available_levels(catalog, 1, admission.PLACE_TYPE_BUDGET) == [2]
    assert admission.available_levels(catalog, 1, admission.PLACE_TYPE_PAID) == [2, 6]
    assert admission.available_levels(catalog, 1, admission.PLACE_TYPE_PAID, max_cost=299_999) == [6]


def test_relevant_groups_filters_paid_by_cost():
    class Client:
        organization_id = 27

        def details(self, education_level, okso_code):
            return [
                {
                    "id": 1,
                    "oksoCode": "2.09.03.01",
                    "oksoName": "Информатика",
                    "educationLevelId": 2,
                    "educationFormId": 1,
                    "placeTypeId": 3,
                    "stageAdmissionId": 1,
                    "numberPlaces": 5,
                    "costOfStudy": 300_000,
                    "entranceTests": TESTS,
                    "programs": [{"id": 1, "name": "ПИ"}],
                }
            ]

    catalog = [
        {"id": 1, "educationLevelId": 2, "educationFormId": 1, "placeTypeId": 3, "stageAdmissionId": 1, "numberPlaces": 5, "oksoCode": "2.09.03.01", "costOfStudy": 300_000},
    ]
    scores = admission.parse_scores(["Русский язык=55", "Математика=72", "Информатика=70"])

    assert len(admission.relevant_groups(Client(), catalog, [2], 1, scores, admission.PLACE_TYPE_PAID)) == 1
    assert len(admission.relevant_groups(Client(), catalog, [2], 1, scores, admission.PLACE_TYPE_PAID, max_cost=250_000)) == 0


def test_analyze_group_includes_paid_place_metadata():
    class Client:
        organization_id = 27

        def applicants(self, competition_id):
            return {"updateDate": "2026-07-19T00:00:00+03:00", "applicants": []}

    group = {
        "id": 123,
        "oksoCode": "2.09.03.01",
        "oksoName": "Информатика и вычислительная техника",
        "educationLevelId": 2,
        "educationLevelName": "Бакалавриат",
        "educationFormId": 1,
        "educationFormName": "Очная",
        "placeTypeId": 3,
        "placeTypeName": "Платные места",
        "costOfStudy": 350_000,
        "numberPlaces": 10,
        "entranceTests": TESTS,
        "programs": [{"id": 99, "name": "Программная инженерия"}],
    }
    scores = admission.parse_scores(
        ["Русский язык=55", "Математика=72", "Информатика=70"]
    )

    result = admission.analyze_group(Client(), group, scores, 5, None, admission.PLACE_TYPE_PAID)

    assert result["placeTypeId"] == 3
    assert result["placeTypeName"] == "Платные места"
    assert result["costOfStudy"] == 350_000
    assert result["seats"] == 10


def test_track_organization_finds_application_and_computes_place():
    class Client:
        organization_id = 27

        def __init__(self, *args, **kwargs):
            pass

        def catalog(self):
            return [
                {
                    "id": 123,
                    "oksoCode": "2.09.03.01",
                    "oksoName": "Информатика",
                    "educationLevelId": 2,
                    "educationLevelName": "Бакалавриат",
                    "educationFormId": 1,
                    "educationFormName": "Очная",
                    "placeTypeId": 1,
                    "placeTypeName": "Бюджет",
                    "stageAdmissionId": 1,
                    "numberPlaces": 4,
                    "programs": [{"id": 99, "name": "Программная инженерия"}],
                }
            ]

        def applicants(self, competition_id):
            return {
                "updateDate": "2026-07-19T00:00:00+03:00",
                "applicants": [
                    {"idApplication": 999, "consent": "ONLINE", "withoutTests": False, "sumMark": 250, "result1": 90, "result2": 85, "result3": 75},
                    {"idApplication": 1281570, "consent": "ONLINE", "withoutTests": False, "sumMark": 202, "result1": 72, "result2": 70, "result3": 55},
                    {"idApplication": 111, "consent": "ONLINE", "withoutTests": False, "sumMark": 200, "result1": 70, "result2": 70, "result3": 55},
                ],
            }

    original_client = admission.GosuslugiClient
    admission.GosuslugiClient = Client
    try:
        organization = {"organizationId": 27, "name": "МЭИ"}
        matches = admission.track_organization(organization, 2026, 1281570, admission.PLACE_TYPE_BUDGET)
    finally:
        admission.GosuslugiClient = original_client

    assert len(matches) == 1
    assert matches[0]["place"] == 2
    assert matches[0]["sumMark"] == 202
    assert matches[0]["specialty"] == "Информатика"
