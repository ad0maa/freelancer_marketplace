class Freelancer < ApplicationRecord
  validates :name, presence: true
  validates :email, presence: true, uniqueness: true
  validates :hourly_rate, numericality: { greater_than_or_equal_to: 0 }
end
