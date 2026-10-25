너는 대화 분석기다. 아래는 엔지니어(user)와 현업 담당자(persona)의 대화 중 마지막 한 턴이다.

[사실 목록]
{{facts_json}}

[직전 대화 맥락 (최근 4턴)]
{{recent_context}}

[이번 user 메시지]
{{user_message}}

[이번 persona 응답]
{{persona_reply}}

판정:
1. disclosed_fact_ids: persona 응답이 엔지니어가 그 사실을 구현에 쓸 수 있을 만큼 구체적으로 전달한 사실 ID 목록. 암시만 했거나 일부만 말했으면 partial_fact_ids에 넣는다.
2. question_specificity (1~5): user 질문의 구체성. 1 = "다 알려주세요"류, 3 = 주제는 분명하나 일반적, 5 = 구체적 장면·사례·기준을 짚은 질문.
3. is_dump_request (bool): 요구사항 전체나 할 일 목록을 한 번에 요구했는가. 특정 주제 없이 "어떻게 하면 돼요?", "필요한 거 다 알려주세요", "자세히 다 설명해 주세요", "할 일을 순서대로 알려주세요"처럼 방법·요구 전체를 통째로 묻는 경우 true.
4. is_confirmation (bool): user가 자기 이해를 정리해 확인을 구했는가.
5. is_scope_proposal (bool): user가 구현 범위를 제안했는가.
6. scope_agreed (bool): persona가 범위 제안에 동의했는가.

JSON 스키마로만 출력한다.
