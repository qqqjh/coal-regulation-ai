import React from 'react'
import { Layout } from 'antd'
import { Outlet } from 'react-router-dom'
import Sidebar from '../Sidebar'
import './index.css'

const { Content } = Layout

const MainLayout = () => {
  return (
    <Layout className="main-layout">
      <Sidebar />
      <Layout>
        <Content className="main-content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}

export default MainLayout
