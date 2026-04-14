"""Korean Law (법령) mode — law.go.kr backed search and citation.

Accuracy-first design:
- Every answer must verbatim-quote article text fetched from law.go.kr.
- No fabrication: if search returns nothing, the answer says so.
- Citations always carry an MST / article code / source URL for back-tracking.
"""
