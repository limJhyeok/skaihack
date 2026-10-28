너는 인지 관리 확인 문제 채점자다. 응시자는 {{time_note}} 코드를 보지 않고 타이핑으로 답했다. 문장력이 아니라 내용을 채점한다.

[질문]
{{question}}

[모범 답안 (세 단계)]
weak_point: {{weak_point}}
impact: {{impact}}
plan: {{plan}}

[관련 코드]
{{code_excerpt}}

[응시자 답변]
{{answer}}

판정:
- steps: weak_point / impact / plan 각각 met(정확히 짚음) / partial(방향은 맞으나 모호하거나 일부) / missed
  - 모범 답안과 다른 약점을 짚었더라도 그것이 실제 코드에 존재하고 타당하면 met으로 인정한다.
  - "코드의 어디를 보면 알 수 있는지, 무엇으로 확인하겠다"고 정확히 말했다면 plan은 partial 이상으로 인정한다.
- misconception: 코드를 잘못 이해한 부분이 있으면 한 문장 (없으면 null)
- revisit: 다시 볼 코드 위치 (파일:줄)
- comment: 응시자에게 줄 한 줄 피드백 (다음엔 어떤 단계를 보강하면 되는지)

모든 문장형 텍스트 값은 한국어로 쓴다. 코드 식별자·파일명·경로·인용문은 원문 그대로 둔다.
JSON 스키마로만 출력한다.
