class Client < ApplicationRecord
  has_many :bookings, dependent: :destroy

  validates :name, presence: true
  validates :email, presence: true, uniqueness: true
end
