/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#0b0d0f',
        paper: '#fafaf7',
        accent: '#2b6cb0',
        muted: '#5a6268',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        serif: ['"Source Serif Pro"', 'Georgia', 'serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      typography: {
        DEFAULT: { css: { maxWidth: '70ch' } },
      },
    },
  },
  plugins: [],
};
