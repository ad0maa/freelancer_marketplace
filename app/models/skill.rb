class Skill < ApplicationRecord
  has_many :freelancer_skills, dependent: :destroy
  has_many :freelancers, through: :freelancer_skills

  validates :name, presence: true, uniqueness: true
end
