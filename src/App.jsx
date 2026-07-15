import { useEffect } from 'react'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import axios from 'axios'
import AppRoutes from './routes'
import useThemeStore from './store/themeStore'
import useUserStore from './store/userStore'
import './App.css'

const applyAxiosAuth = (accessToken) => {
  if (accessToken) {
    axios.defaults.headers.common.Authorization = `Bearer ${accessToken}`
  } else {
    delete axios.defaults.headers.common.Authorization
  }
}

// Configure authentication before child pages mount and issue their first request.
applyAxiosAuth(useUserStore.getState().accessToken)
useUserStore.subscribe((state, previousState) => {
  if (state.accessToken !== previousState.accessToken) {
    applyAxiosAuth(state.accessToken)
  }
})

function App() {
  const currentTheme = useThemeStore((state) => state.theme)

  // 同步主题到 document
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', currentTheme)
  }, [currentTheme])

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: currentTheme === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      <AppRoutes />
    </ConfigProvider>
  )
}

export default App
