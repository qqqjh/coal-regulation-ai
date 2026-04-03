import { create } from 'zustand'
import { persist } from 'zustand/middleware'

const useUserStore = create(
  persist(
    (set) => ({
      // 用户信息
      user: null,
      // 是否已登录
      isLoggedIn: false,

      // 登录
      login: (userInfo) => {
        set({
          user: userInfo,
          isLoggedIn: true
        })
      },

      // 退出登录
      logout: () => {
        set({
          user: null,
          isLoggedIn: false
        })
      },

      // 更新用户信息
      updateUser: (userInfo) => {
        set((state) => ({
          user: { ...state.user, ...userInfo }
        }))
      }
    }),
    {
      name: 'user-storage', // localStorage 中的 key
    }
  )
)

export default useUserStore
