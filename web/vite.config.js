import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Store-backed endpoints live in the stdlib server (omp_memory.web);
      // run it on 4174 in dev. Graph cosmetics stay on the express server.
      '/api/record': 'http://localhost:4174',
      '/api/stats': 'http://localhost:4174',
      '/api/image': 'http://localhost:4174',
      '/media': 'http://localhost:4174',
      '/api': 'http://localhost:4001',
      '/socket.io': { target: 'http://localhost:4001', ws: true },
      '/events': { target: 'http://localhost:4001' },
    },
  },
})
