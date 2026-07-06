import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'
export default defineConfig({plugins:[react()],build:{outDir:resolve(__dirname,'../src/runrail/web/static'),emptyOutDir:true},server:{proxy:{'/api':'http://127.0.0.1:8080'}}})
