import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

import App from './App.vue'
import { restoreSession } from './api'
import { createAppRouter } from './router'
import './styles.css'

await restoreSession()

createApp(App).use(ElementPlus).use(createAppRouter()).mount('#app')
