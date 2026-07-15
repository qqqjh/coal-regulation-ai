import { useEffect } from 'react'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import axios from 'axios'
import AppRoutes from './routes'
import useThemeStore from './store/themeStore'
import useUserStore from './store/userStore'
import './App.css'

function App() {
  const currentTheme = useThemeStore((state) => state.theme)
  const accessToken = useUserStore((state) => state.accessToken)

  // 同步主题到 document
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', currentTheme)
  }, [currentTheme])

  useEffect(() => {
    if (accessToken) {
      axios.defaults.headers.common.Authorization = `Bearer ${accessToken}`
    } else {
      delete axios.defaults.headers.common.Authorization
    }
  }, [accessToken])

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
