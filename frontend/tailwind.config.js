/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{js,ts,jsx,tsx}',
    './components/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        sgr: {
          dark: '#0f1419',
          light: '#f5f7fa',
          accent: '#2563eb',
          success: '#10b981',
          danger: '#ef4444',
          warning: '#f59e0b',
        },
      },
    },
  },
  plugins: [],
}
