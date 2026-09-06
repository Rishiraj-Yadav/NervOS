---
paths:
  - "apps/web/**/*.{ts,tsx,css}"
---

# NervOS Frontend Rules

- TypeScript strict mode must remain enabled.
- Do not use `any` unless documented and unavoidable.
- Server state belongs in TanStack Query.
- Do not duplicate API state in arbitrary React state.
- Authentication must be cookie-based.
- Do not store auth credentials in localStorage.
- API access goes through the shared API client.
- Pages should compose components rather than contain large business logic blocks.
- Loading, empty, error, and success states must be handled.
- User-facing errors must not expose raw backend stack traces.
- Prefer accessible semantic HTML.
- Add tests for important behavior.