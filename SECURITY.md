# Security (implemented vs. not)
Implemented: scrypt password hashing; JWT (HS256) with expiry; secret only from env (app refuses to start without it); role checks on every protected route;
every query filtered by org_id (cross-org tests); parameterized SQL, whitelisted sort columns; input validation and size limit (1 MB);
login throttling (5 tries/5 min, in-memory); CSV formula-injection escaping; CSP `default-src 'self'`, X-Frame-Options, nosniff; UI renders data via textContent (no innerHTML);
generic 404/500 errors; audit trail via activity table; no passwords/tokens logged.
Not yet: CSRF (not needed for Bearer-header auth, revisit if cookies are adopted); token revocation/refresh; shared rate-limit store; HTTPS/HSTS (set at the proxy);
Postgres row-level security; secret rotation; dependency audit; third-party pentest. Token is kept in sessionStorage (XSS would expose it; CSP mitigates).
