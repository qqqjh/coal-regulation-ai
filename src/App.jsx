import { useEffect } from 'react'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import AppRoutes from './routes'
import useThemeStore from './store/themeStore'
import './App.css'

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
