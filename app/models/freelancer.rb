class Freelancer < ApplicationRecord
  has_many :freelancer_skills, dependent: :destroy
  has_many :skills, through: :freelancer_skills

  validates :name, presence: true
  validates :email, presence: true, uniqueness: true
  validates :hourly_rate, numericality: { greater_than_or_equal_to: 0 }
end
