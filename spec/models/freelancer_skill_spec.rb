require 'rails_helper'

RSpec.describe FreelancerSkill, type: :model do
  describe 'validations' do
    it 'is valid with a freelancer and a skill' do
      freelancer_skill = described_class.new(
        freelancer: create(:freelancer),
        skill: create(:skill)
      )
      expect(freelancer_skill).to be_valid
    end

    it 'is invalid without a freelancer' do
      freelancer_skill = described_class.new(freelancer: nil, skill: create(:skill))
      expect(freelancer_skill).not_to be_valid
    end

    it 'is invalid without a skill' do
      freelancer_skill = described_class.new(freelancer: create(:freelancer), skill: nil)
      expect(freelancer_skill).not_to be_valid
    end

    it 'prevents duplicate freelancer-skill combinations' do
      freelancer = create(:freelancer)
      skill = create(:skill)
      described_class.create!(freelancer: freelancer, skill: skill)

      duplicate = described_class.new(freelancer: freelancer, skill: skill)
      expect(duplicate).not_to be_valid
    end
  end
end
