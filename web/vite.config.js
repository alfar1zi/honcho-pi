import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://localhost:4001',
      '/socket.io': { target: 'http://localhost:4001', ws: true },
      '/events': { target: 'http://localhost:4001' },
    },
  },
})
