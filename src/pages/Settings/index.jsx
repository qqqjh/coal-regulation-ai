import { Card, Col, Descriptions, Row, Select, Switch } from 'antd'
import {
  BellOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  SkinOutlined,
} from '@ant-design/icons'
import useThemeStore from '../../store/themeStore'
import useUserStore from '../../store/userStore'
import './index.css'

export default function Settings() {
  const user = useUserStore(state => state.user)
  const theme = useThemeStore(state => state.theme)
  const setTheme = useThemeStore(state => state.setTheme)

  return (
    <div className="settings-page">
      <div className="settings-header">
        <h1>设置</h1>
        <p>配置界面偏好、审查默认项和账号权限视图。</p>
      </div>

      <Row gutter={[20, 20]}>
        <Col xs={24} lg={12}>
          <Card
            className="settings-card"
            title={<span><SkinOutlined /> 界面偏好</span>}
          >
            <div className="settings-row">
              <div>
                <div className="settings-row-title">主题模式</div>
                <div className="settings-row-desc">切换浅色或深色界面</div>
              </div>
              <Select
                value={theme}
                style={{ width: 140 }}
                onChange={setTheme}
                options={[
                  { value: 'light', label: '浅色模式' },
                  { value: 'dark', label: '深色模式' },
                ]}
              />
            </div>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">紧凑信息密度</div>
                <div className="settings-row-desc">适合长文档审查的密集布局</div>
              </div>
              <Switch defaultChecked disabled />
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            className="settings-card"
            title={<span><SafetyCertificateOutlined /> 审查默认项</span>}
          >
            <div className="settings-row">
              <div>
                <div className="settings-row-title">默认矿井类型</div>
                <div className="settings-row-desc">多智能体审查上传时的默认选项</div>
              </div>
              <Select
                defaultValue="non_outburst"
                style={{ width: 140 }}
                options={[
                  { value: 'non_outburst', label: '非突出矿井' },
                  { value: 'outburst', label: '突出矿井' },
                ]}
              />
            </div>
            <div className="settings-row">
              <div>
                <div className="settings-row-title">历史任务提醒</div>
                <div className="settings-row-desc">审查完成后在历史区域保留任务入口</div>
              </div>
              <Switch defaultChecked disabled />
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            className="settings-card"
            title={<span><BellOutlined /> 通知</span>}
          >
            <div className="settings-row">
              <div>
                <div className="settings-row-title">前端消息提示</div>
                <div className="settings-row-desc">上传、审查、反馈状态用消息提示</div>
              </div>
              <Switch defaultChecked disabled />
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card
            className="settings-card"
            title={<span><SettingOutlined /> 当前账号</span>}
          >
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="用户名">{user?.username || '-'}</Descriptions.Item>
              <Descriptions.Item label="角色">
                {user?.role === 'admin' ? '管理员' : '普通用户'}
              </Descriptions.Item>
              <Descriptions.Item label="可用模块">
                知识库、RAG、多智能体审查、用量监控
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>
    </div>
  )
}
