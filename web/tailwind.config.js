/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["-apple-system", "BlinkMacSystemFont", '"Segoe UI"', "Roboto", "sans-serif"],
      },
      colors: {
        accent: {
          DEFAULT: "#4f46e5", // indigo-600
        },
      },
    },
  },
  plugins: [],
};
