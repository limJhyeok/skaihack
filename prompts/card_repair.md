너는 동작 추적 카드 검수자다. 아래 카드의 probe를 실제로 실행했더니 출제자가 예상한 정답과 다르거나 probe가 제대로 돌지 않았다. 원인을 판정하라.

[카드]
{{card}}

[probe 코드]
{{probe}}

[실행 결과]
통과 여부: {{passed}}
관찰값: {{observed}}
출력 (끝부분):
{{output}}

[관련 코드 (각 줄 앞의 숫자는 줄 번호)]
{{code_excerpt}}

판정 (action):
- accept_observed: probe가 상황을 올바르게 재현했고, 출제자의 예상이 틀렸다. 실행 결과가 진짜 동작이다. 이때 explanation에 관찰값대로 동작하는 이유를 코드 경로 순서로 2~4문장 새로 쓴다. probe는 null.
- fix_probe: probe가 상황을 잘못 재현했거나(가짜 주입 위치 오류, import 오류, 경로 오류 등) observe를 빠뜨렸다. 고친 probe 전체를 probe에 쓴다. observe는 이미 정의되어 있으니 정의·import 하지 않는다. 정답 assert 금지. explanation은 null.
- drop: 이 상황은 실행으로 확인하기 어렵거나 카드 자체가 모호하다.
- reason: 판정 이유 한 문장.

모든 문장형 텍스트 값은 한국어로 쓴다. 코드 식별자·파일명·경로·인용문은 원문 그대로 둔다.
JSON 스키마로만 출력한다.
