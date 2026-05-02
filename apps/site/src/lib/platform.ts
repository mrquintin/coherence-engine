export const platformLinks = {
  founder:
    import.meta.env.PUBLIC_FOUNDER_PORTAL_URL ??
    'https://coherence-founder-portal.vercel.app',
  partner:
    import.meta.env.PUBLIC_PARTNER_DASHBOARD_URL ??
    'https://coherence-partner-dashboard.vercel.app',
  lp:
    import.meta.env.PUBLIC_LP_PORTAL_URL ??
    'https://coherence-lp-portal.vercel.app',
  api:
    import.meta.env.PUBLIC_API_URL ?? 'https://coherence-fund-api.vercel.app',
};
