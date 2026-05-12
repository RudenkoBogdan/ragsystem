import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: "#0f1117",
          secondary: "#1a1d27",
          tertiary: "#242837",
        },
        accent: {
          blue: "#4f8ef7",
          "blue-hover": "#6ba3f8",
        },
        border: "#2e3347",
        text: {
          primary: "#e8eaf0",
          secondary: "#8b90a4",
          muted: "#555b72",
        },
      },
    },
  },
  plugins: [],
};

export default config;
